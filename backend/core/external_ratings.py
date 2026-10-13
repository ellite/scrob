"""Third-party ratings (IMDb, Rotten Tomatoes, Metacritic, ...) via MDBList.

MDBList's per-title endpoint (``GET /tmdb/{movie|show}/{id}``) returns every
source in one ``ratings`` array, so one call fills one ``external_ratings``
row for all sources. Rows are shared across users and refreshed at most once
per FRESH_FOR; a failed refresh keeps serving the old values.

Quota: the free MDBList tier allows 1000 requests/day. Titles MDBList doesn't
know are cached as empty (no retry until FRESH_FOR passes), concurrent requests
for the same title share one call, and after a daily-limit 429 every fetch is
skipped for an hour.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core import mdblist as mdblist_client
from models.external_rating import ExternalRating
from models.global_settings import GlobalSettings
from models.users import UserSettings

logger = logging.getLogger(__name__)

# Scrob source key -> MDBList ``source`` names (first match wins).
SOURCES: dict[str, tuple[str, ...]] = {
    "imdb": ("imdb",),
    "tomatometer": ("tomatoes",),
    "popcornmeter": ("popcorn", "tomatoesaudience"),
    "metacritic": ("metacritic",),
    "letterboxd": ("letterboxd",),
    "trakt": ("trakt",),
}
MEDIA_TYPES = {"movie": "movie", "tv": "show", "series": "show", "show": "show"}

FRESH_FOR = timedelta(hours=24)
KEEP_FOR = timedelta(days=30)        # how far back the page bootstrap map looks
FETCH_WAIT_SECONDS = 4.0             # a page never waits longer than this for MDBList
DAILY_LIMIT_PAUSE_SECONDS = 3600
RATE_LIMIT_PAUSE_SECONDS = 60

_paused_until = 0.0
_inflight: dict[tuple[str, int], asyncio.Task] = {}

_URL_BASES = {
    "tomatometer": "https://www.rottentomatoes.com",
    "popcornmeter": "https://www.rottentomatoes.com",
}


def _url(source: str, raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("https://"):
        return raw
    base = _URL_BASES.get(source)
    return base + raw if base and raw.startswith("/") else None


def parse(data: Any) -> dict[str, dict[str, Any]]:
    """MDBList item response -> {source: {"value", "votes", "url"}}; sources without a value are left out."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return out
    by_name = {r.get("source"): r for r in data.get("ratings") or [] if isinstance(r, dict)}
    for key, names in SOURCES.items():
        for name in names:
            entry = by_name.get(name)
            if not entry or entry.get("value") is None:
                continue
            try:
                value = float(entry["value"])
            except (TypeError, ValueError):
                continue
            votes = entry.get("votes")
            out[key] = {
                "value": value,
                "votes": int(votes) if isinstance(votes, (int, float)) else None,
                "url": _url(key, entry.get("url")),
            }
            break
    return out


async def resolve_api_key(db: AsyncSession, user_id: int) -> str | None:
    """The user's own MDBList key, else the admin's global one."""
    user_key = (await db.execute(
        select(UserSettings.mdblist_api_key).where(UserSettings.user_id == user_id)
    )).scalar_one_or_none()
    if user_key:
        return user_key
    return (await db.execute(
        select(GlobalSettings.mdblist_api_key).where(GlobalSettings.id == 1)
    )).scalar_one_or_none()


async def _load(db: AsyncSession, media_type: str, tmdb_id: int) -> ExternalRating | None:
    return (await db.execute(
        select(ExternalRating).where(ExternalRating.media_type == media_type, ExternalRating.tmdb_id == tmdb_id)
    )).scalar_one_or_none()


async def _fetch_and_store(api_key: str, media_type: str, tmdb_id: int) -> dict[str, dict[str, Any]] | None:
    """One MDBList call + upsert. Runs in its own session so it can outlive the request that started it."""
    global _paused_until
    try:
        data = await mdblist_client._request_json(
            "GET", f"/tmdb/{media_type}/{tmdb_id}", api_key,
            ignore_statuses={404}, rate_limit_retries=0,
        )
    except mdblist_client.MDBListDailyLimitError:
        _paused_until = time.monotonic() + DAILY_LIMIT_PAUSE_SECONDS
        logger.info("MDBList daily limit reached; external ratings paused for an hour")
        return None
    except mdblist_client.MDBListAPIError as exc:
        if "(429)" in str(exc):
            _paused_until = time.monotonic() + RATE_LIMIT_PAUSE_SECONDS
        logger.debug("External ratings fetch failed for %s/%s: %s", media_type, tmdb_id, exc)
        return None

    ratings = parse(data)
    from db import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        stmt = insert(ExternalRating).values(
            media_type=media_type, tmdb_id=tmdb_id, ratings=ratings, fetched_at=datetime.utcnow(),
        ).on_conflict_do_update(
            constraint="uq_external_ratings_title",
            set_={"ratings": ratings, "fetched_at": datetime.utcnow()},
        )
        await session.execute(stmt)
        await session.commit()
    return ratings


async def get_ratings(
    db: AsyncSession, api_key: str | None, media_type: str, tmdb_id: int,
) -> dict[str, dict[str, Any]]:
    """All cached sources for a title, refreshing from MDBList when stale.

    Never waits more than FETCH_WAIT_SECONDS for MDBList: on timeout the old
    values (or {}) are returned and the fetch finishes in the background.
    """
    row = await _load(db, media_type, tmdb_id)
    cached = dict(row.ratings) if row else {}
    if row and datetime.utcnow() - row.fetched_at < FRESH_FOR:
        return cached
    if not api_key or time.monotonic() < _paused_until:
        return cached

    key = (media_type, tmdb_id)
    task = _inflight.get(key)
    if task is None:
        task = asyncio.get_running_loop().create_task(_fetch_and_store(api_key, media_type, tmdb_id))
        _inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _inflight.pop(k, None))
    try:
        fresh = await asyncio.wait_for(asyncio.shield(task), FETCH_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return cached
    return cached if fresh is None else fresh


async def cached_map(db: AsyncSession, source: str) -> tuple[dict[str, float], list[str], list[str]]:
    """Every cached value of one source from the last KEEP_FOR, for the page bootstrap.

    Returns (values, stale, missing) with keys like "m123" (movie) / "s45" (show):
    values  - {"m123": 94.0}
    stale   - older than FRESH_FOR: shown, but refreshed by the browser when visible
    missing - fetched, but MDBList has no value for this source (badge falls back to TMDB)
    """
    now = datetime.utcnow()
    rows = (await db.execute(
        select(ExternalRating.media_type, ExternalRating.tmdb_id, ExternalRating.ratings, ExternalRating.fetched_at)
        .where(ExternalRating.fetched_at >= now - KEEP_FOR)
    )).all()
    values: dict[str, float] = {}
    stale: list[str] = []
    missing: list[str] = []
    for media_type, tmdb_id, ratings, fetched_at in rows:
        short = ("m" if media_type == "movie" else "s") + str(tmdb_id)
        entry = (ratings or {}).get(source)
        if entry and entry.get("value") is not None:
            values[short] = entry["value"]
        else:
            missing.append(short)
        if now - fetched_at >= FRESH_FOR:
            stale.append(short)
    return values, stale, missing
