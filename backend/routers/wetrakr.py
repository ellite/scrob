"""WeTrakr integration router.

Endpoints:
  POST   /wetrakr/auth/device/start  – Start device auth flow
  POST   /wetrakr/auth/device/poll   – Poll for token completion
  DELETE /wetrakr/auth/disconnect    – Revoke token and clear stored credentials
  POST   /wetrakr/sync               – Trigger a WeTrakr import (watched history + ratings)
  POST   /wetrakr/push               – Push Scrob history/ratings to WeTrakr

Unlike Trakt/Simkl there's no per-user client_id/secret to configure — Scrob
ships a single app-owned key (core/wetrakr.py: WETRAKR_CLIENT_ID), so every
endpoint here only ever gates on whether the user is connected.
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import wetrakr as wetrakr_client
from core.enrichment import enrich_media, is_unmapped_tvdb_episode, create_media_safely
from core.rewatch import record_rewatch_progress
from db import get_db, engine
from dependencies import get_current_user
from models.base import CollectionSource, MediaType
from models.comments import Comment
from models.events import WatchEvent
from models.lists import List as ListModel, ListItem
from models.media import Media
from models.ratings import Rating, RatingChanges
from models.show import Show
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings
from models.global_settings import GlobalSettings

logger = logging.getLogger(__name__)

router = APIRouter()

TMDB_CONCURRENCY = 10
# WeTrakr accepts 5,000 items per write. A 50-item chunk turned a library of
# roughly 2,200 plays into the observed burst of 44 POST /sync/tracking calls.
WETRAKR_PUSH_BATCH_SIZE = 5000
# A batch that times out may or may not have been applied. Resending it is
# safe (allow_rewatch stays false, so applied items come back as "Already
# watched" and a re-rated title just keeps its rating), so it is resent in
# smaller pieces that are quicker for the server to answer.
WETRAKR_PUSH_RETRY_BATCH_SIZE = 1000


def _watched_sender(access_token: str, allow_rewatch: bool = False) -> Callable[[list[tuple[str, tuple]]], Awaitable[None]]:
    async def send(chunk: list[tuple[str, tuple]]) -> None:
        movies = [item for kind, item in chunk if kind == "movie"]
        episodes = [item for kind, item in chunk if kind == "episode"]
        await wetrakr_client.add_to_watched_batch(access_token, movies, episodes, allow_rewatch)
    return send


def _ratings_sender(access_token: str) -> Callable[[list[tuple[str, tuple]]], Awaitable[None]]:
    async def send(chunk: list[tuple[str, tuple]]) -> None:
        movies = [item for kind, item in chunk if kind == "movie"]
        shows = [item for kind, item in chunk if kind == "show"]
        await wetrakr_client.set_ratings_batch(access_token, movies, shows)
    return send


async def _send_chunk(send: Callable[[list], Awaitable[None]], chunk: list) -> None:
    """Send one write batch; if it times out, resend it in smaller pieces."""
    try:
        await send(chunk)
        return
    except httpx.TimeoutException:
        if len(chunk) <= WETRAKR_PUSH_RETRY_BATCH_SIZE:
            raise
        logger.warning(
            "WeTrakr write of %d items timed out, resending in batches of %d",
            len(chunk), WETRAKR_PUSH_RETRY_BATCH_SIZE,
        )
    for i in range(0, len(chunk), WETRAKR_PUSH_RETRY_BATCH_SIZE):
        await send(chunk[i:i + WETRAKR_PUSH_RETRY_BATCH_SIZE])


async def _send_in_chunks(items: list, send: Callable[[list], Awaitable[None]]) -> None:
    for i in range(0, len(items), WETRAKR_PUSH_BATCH_SIZE):
        await _send_chunk(send, items[i:i + WETRAKR_PUSH_BATCH_SIZE])

# WeTrakr device-auth access tokens live 7 days. Refresh once we're within
# this of expiry so a job that starts near the boundary doesn't race it —
# same rationale as Trakt's _TRAKT_TOKEN_REFRESH_SKEW (see routers/trakt.py).
_WETRAKR_TOKEN_REFRESH_SKEW = timedelta(days=1)


def _reset_wetrakr_bookmarks(settings: UserSettings) -> None:
    """Forget what the incremental pull/push last saw, so the next run is a
    full one."""
    settings.wetrakr_last_activity = None
    settings.wetrakr_last_push_at = None
    settings.wetrakr_last_lists_push_at = None


_JOB_SLOT_LOCK_KEY = 0x57455452  # "WETR": first half of the advisory lock key


async def _claim_job_slot(db: AsyncSession, user_id: int) -> None:
    """Refuse (409) when this user already has a WeTrakr job pending or running.

    The advisory lock is held until the caller's next commit, so two requests
    arriving together cannot both pass the check before either has inserted its
    job. No-op on the sqlite engine some unit tests substitute for Postgres.
    """
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is not None and bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key, :user_id)"),
            {"key": _JOB_SLOT_LOCK_KEY, "user_id": user_id},
        )
    active = await db.execute(
        select(SyncJob.id).where(
            SyncJob.user_id == user_id,
            SyncJob.source == CollectionSource.wetrakr,
            SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]),
        ).limit(1)
    )
    if active.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="A WeTrakr sync is already running")


class WeTrakrTokenError(Exception):
    """The stored WeTrakr token is unusable and can't be refreshed automatically -
    the user needs to reconnect WeTrakr in Settings. The message is safe to show."""


async def ensure_valid_wetrakr_token(
    db: AsyncSession, settings: UserSettings | None, *, force_check: bool = False
) -> str:
    """Return a usable WeTrakr access token, refreshing and persisting it first
    if it's expired or close to it. Raises WeTrakrTokenError when WeTrakr isn't
    connected or a needed refresh isn't possible / fails. Every path that calls
    WeTrakr with the stored token must go through this (see ensure_valid_trakt_token
    in routers/trakt.py for why — the scheduled push not doing so is a known trap)."""
    if not settings or not settings.wetrakr_access_token:
        raise WeTrakrTokenError("WeTrakr is not connected.")

    now = int(datetime.now(timezone.utc).timestamp())
    expires_at = settings.wetrakr_token_expires_at
    if not force_check and expires_at and expires_at - now > _WETRAKR_TOKEN_REFRESH_SKEW.total_seconds():
        return settings.wetrakr_access_token

    if await wetrakr_client.validate_token(settings.wetrakr_access_token):
        return settings.wetrakr_access_token

    if not settings.wetrakr_refresh_token:
        raise WeTrakrTokenError("WeTrakr token expired. Please reconnect WeTrakr in Settings.")

    try:
        token_data = await wetrakr_client.refresh_access_token(settings.wetrakr_refresh_token)
    except Exception as exc:
        logger.warning("WeTrakr token refresh failed for user %s: %s", settings.user_id, exc)
        raise WeTrakrTokenError(f"WeTrakr token expired and the automatic refresh failed: {exc}") from exc

    settings.wetrakr_access_token = token_data["access_token"]
    # WeTrakr's refresh endpoint returns the rotated refresh token under
    # new_refresh_token, not refresh_token — the docs flag this explicitly.
    settings.wetrakr_refresh_token = token_data.get("new_refresh_token") or token_data.get("refresh_token")
    settings.wetrakr_token_expires_at = token_data.get("expires_in", 0) + int(datetime.now(timezone.utc).timestamp())
    await db.commit()
    logger.info("Refreshed WeTrakr access token for user %s", settings.user_id)
    return settings.wetrakr_access_token


# ── Device Authentication ─────────────────────────────────────────────────────

@router.post("/auth/device/start")
async def wetrakr_device_start(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiate device authentication. Returns user_code + verification_url."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = UserSettings(user_id=current_user.id)
        db.add(settings)

    data = await wetrakr_client.start_device_auth()

    settings.wetrakr_device_code = data["device_code"]
    await db.commit()

    return {
        "user_code": data["user_code"],
        "verification_url": data["verification_url"],
        "expires_in": data["expires_in"],
        "interval": data["interval"],
    }


@router.post("/auth/device/poll")
async def wetrakr_device_poll(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check if the user has authorized the device. Call repeatedly per the interval."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if not settings or not settings.wetrakr_device_code:
        raise HTTPException(status_code=400, detail="No pending device authorization. Call /auth/device/start first.")

    try:
        token_data = await wetrakr_client.poll_device_token(settings.wetrakr_device_code)
    except Exception as exc:
        settings.wetrakr_device_code = None
        await db.commit()
        raise HTTPException(status_code=400, detail=f"Authorization failed: {exc}")

    if token_data is None:
        return {"status": "pending"}

    settings.wetrakr_access_token = token_data["access_token"]
    settings.wetrakr_refresh_token = token_data["refresh_token"]
    settings.wetrakr_token_expires_at = token_data.get("expires_in", 0) + int(datetime.now(timezone.utc).timestamp())
    settings.wetrakr_device_code = None
    # The bookmarks belong to whichever account was connected before.
    _reset_wetrakr_bookmarks(settings)
    await db.commit()

    return {"status": "connected"}


@router.delete("/auth/disconnect")
async def wetrakr_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revoke the WeTrakr session and clear stored credentials."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if settings and settings.wetrakr_access_token:
        if settings.wetrakr_refresh_token:
            await wetrakr_client.revoke_token(settings.wetrakr_access_token, settings.wetrakr_refresh_token)
        settings.wetrakr_access_token = None
        settings.wetrakr_refresh_token = None
        settings.wetrakr_token_expires_at = None
        settings.wetrakr_device_code = None
        _reset_wetrakr_bookmarks(settings)
        await db.commit()

    return {"status": "disconnected"}


# ── Sync helpers (shared with run_wetrakr_sync) ───────────────────────────────

async def _get_or_create_show(db: AsyncSession, tmdb_id: int, title: str, api_key: str | None) -> Show | None:
    result = await db.execute(select(Show).where(Show.tmdb_id == tmdb_id))
    show = result.scalars().first()
    if show:
        return show
    from core import tmdb
    try:
        d = await tmdb.get_show(tmdb_id, api_key=api_key)
        show = Show(
            tmdb_id=tmdb_id,
            title=d.get("name") or title,
            original_title=d.get("original_name"),
            overview=d.get("overview"),
            poster_path=tmdb.poster_url(d.get("poster_path")),
            backdrop_path=tmdb.poster_url(d.get("backdrop_path"), size="w1280"),
            tmdb_rating=d.get("vote_average"),
            status=d.get("status"),
            tagline=d.get("tagline"),
            first_air_date=d.get("first_air_date"),
            last_air_date=d.get("last_air_date"),
            tmdb_data={
                "genres": [g["name"] for g in d.get("genres", [])],
                "external_ids": d.get("external_ids", {}),
                "original_language": d.get("original_language"),
                "seasons": [
                    {
                        "season_number": s["season_number"],
                        "poster_path": tmdb.poster_url(s.get("poster_path")),
                        "episode_count": s["episode_count"],
                        "name": s["name"],
                    }
                    for s in d.get("seasons", [])
                ],
            },
        )
        db.add(show)
        await db.flush()
        return show
    except Exception as exc:
        logger.warning("Could not fetch show tmdb=%s: %s", tmdb_id, exc)
        return None


async def _get_or_create_movie_media(db: AsyncSession, tmdb_id: int, title: str, api_key: str | None) -> Media | None:
    result = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.movie)
    )
    media = result.scalars().first()
    if media:
        return media
    media, _created = await create_media_safely(db, tmdb_id, MediaType.movie, title=title)
    await enrich_media(media, api_key=api_key)
    return media


async def _get_or_create_series_media(db: AsyncSession, tmdb_id: int, title: str, api_key: str | None) -> Media | None:
    result = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.series)
    )
    media = result.scalars().first()
    if media:
        return media
    media, _created = await create_media_safely(db, tmdb_id, MediaType.series, title=title)
    await enrich_media(media, api_key=api_key)
    return media


async def _get_or_create_episode_media(
    db: AsyncSession,
    show_id: int,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
    api_key: str | None,
) -> Media | None:
    result = await db.execute(
        select(Media).where(
            Media.show_id == show_id,
            Media.season_number == season_number,
            Media.episode_number == episode_number,
            Media.media_type == MediaType.episode,
        )
    )
    media = result.scalars().first()
    if media:
        return media
    from core import tmdb
    try:
        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)
        async with semaphore:
            season_data = await tmdb.get_season(show_tmdb_id, season_number, api_key=api_key)
        ep_map = {ep["episode_number"]: ep for ep in season_data.get("episodes", [])}
        ep = ep_map.get(episode_number)
        if not ep:
            # TMDB has no such episode (provider numbering mismatch) — don't fabricate
            # a placeholder row for it; see routers/trakt.py's twin of this function.
            logger.warning(
                "WeTrakr episode s%se%s not found on TMDB for show tmdb=%s — skipping",
                season_number, episode_number, show_tmdb_id,
            )
            return None
        media, _created = await create_media_safely(
            db,
            ep["id"],
            MediaType.episode,
            title=ep["name"],
            overview=ep.get("overview"),
            poster_path=tmdb.poster_url(ep.get("still_path"), size="w500"),
            release_date=ep.get("air_date"),
            tmdb_rating=ep.get("vote_average"),
            runtime=ep.get("runtime"),
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number,
            tmdb_data={"runtime": ep.get("runtime"), "cast": []},
        )
        return media
    except Exception as exc:
        logger.warning("Could not fetch episode s%se%s for show tmdb=%s: %s", season_number, episode_number, show_tmdb_id, exc)
        return None


def _parse_wetrakr_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from dateutil import parser as dt_parser
        dt = dt_parser.isoparse(value)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _wetrakr_media_tmdb_id(media_obj: dict) -> int | None:
    tmdb_id = (media_obj.get("ids") or {}).get("tmdb")
    return int(tmdb_id) if tmdb_id else None


def _wetrakr_user_rating(item: dict) -> tuple[float | None, str | None]:
    """Extract (rating, rated_at) from a /sync/ratings/{target} media object.

    interactions.user.rating is itself an object ({rating, rated_at}), not a
    bare number — the numeric value is nested one level further under its own
    "rating" key alongside "rated_at" (confirmed against the live API; the
    docs' wording is ambiguous enough that the flat-number reading seemed
    equally plausible and turned out wrong)."""
    # Compact sync rows expose these fields directly. Keep accepting the full
    # media shape for compatibility with older API responses.
    direct_rating = item.get("rating")
    if isinstance(direct_rating, (int, float)):
        return (float(direct_rating), item.get("rated_at"))
    user = ((item.get("interactions") or {}).get("user") or {})
    rating_obj = user.get("rating")
    if not isinstance(rating_obj, dict):
        return (None, None)
    rating = rating_obj.get("rating")
    return (float(rating) if rating else None, rating_obj.get("rated_at"))


async def _resolve_wetrakr_comment_target(
    comment: dict,
    caches: dict[str, dict[int, dict]],
) -> tuple[str, int, Optional[int], Optional[int]] | None:
    """Resolve a comment's compact target reference (WeTrakr-id only) to
    (media_type, tmdb_id, season_number, episode_number), matching the same
    convention Comment.tmdb_id already uses for Trakt imports (see
    _extract_trakt_comment): tmdb_id is the SHOW's id for season/episode
    comments, never the season/episode's own id.

    `caches` holds one dict per WeTrakr object type so repeated comments on
    the same title only fetch it once per sync run. Returns None for
    person/list targets (no Scrob equivalent) or when the title no longer
    exists. A transient failure (rate limit, timeout, 5xx) is raised so the
    caller can leave the comment for the next pull.
    """
    target = comment.get("target")
    try:
        if target == "movie":
            wid = (comment.get("movie") or {}).get("id")
            if not wid:
                return None
            cache = caches.setdefault("movie", {})
            data = cache.get(wid) or await wetrakr_client.get_movie(wid)
            cache[wid] = data
            tmdb_id = (data.get("ids") or {}).get("tmdb")
            return ("movie", int(tmdb_id), None, None) if tmdb_id else None

        if target == "show":
            wid = (comment.get("show") or {}).get("id")
            if not wid:
                return None
            cache = caches.setdefault("show", {})
            data = cache.get(wid) or await wetrakr_client.get_show(wid)
            cache[wid] = data
            tmdb_id = (data.get("ids") or {}).get("tmdb")
            return ("series", int(tmdb_id), None, None) if tmdb_id else None

        if target == "season":
            wid = (comment.get("season") or {}).get("id")
            if not wid:
                return None
            cache = caches.setdefault("season", {})
            data = cache.get(wid) or await wetrakr_client.get_season(wid)
            cache[wid] = data
            show_tmdb_id = ((data.get("show") or {}).get("ids") or {}).get("tmdb")
            season_number = data.get("number")
            if not show_tmdb_id or season_number is None:
                return None
            return ("series", int(show_tmdb_id), int(season_number), None)

        if target == "episode":
            wid = (comment.get("episode") or {}).get("id")
            if not wid:
                return None
            cache = caches.setdefault("episode", {})
            data = cache.get(wid) or await wetrakr_client.get_episode(wid)
            cache[wid] = data
            show_tmdb_id = ((data.get("show") or {}).get("ids") or {}).get("tmdb")
            # Unconfirmed field name (see core.wetrakr.get_episode) — try the
            # flat field first, then a nested season object, before giving up.
            season_number = data.get("season_number")
            if season_number is None:
                season_number = (data.get("season") or {}).get("number")
            episode_number = data.get("number")
            if not show_tmdb_id or season_number is None or episode_number is None:
                return None
            return ("episode", int(show_tmdb_id), int(season_number), int(episode_number))
    except httpx.HTTPStatusError as exc:
        # A 404/410 means the title is gone: nothing to resolve, ever. Anything
        # else (429, 5xx) is transient and must not mark the comment as seen.
        if exc.response.status_code in (404, 410):
            logger.warning("WeTrakr comment target (%s) no longer exists: %s", target, exc)
            return None
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        logger.warning("Could not resolve WeTrakr comment target (%s, id=%s): %s", target, comment.get(target, {}).get("id") if isinstance(comment.get(target), dict) else None, exc)
        return None

    return None  # target is "person" or "list" — no Scrob equivalent


def _section_changed(activities: dict, section: str, key: str, since: str | None) -> bool:
    """True when `section` of GET /sync/last_activities moved after `since`
    (the "all" stamp stored by the last pull). Anything unknown reads as
    changed: no stored stamp, a failed last_activities call, or a stamp that
    does not parse."""
    if not since:
        return True
    block = activities.get(section)
    if not isinstance(block, dict):
        return True
    stamp = block.get(key) or block.get("all")
    if not isinstance(stamp, str):
        return True
    current, previous = _parse_wetrakr_datetime(stamp), _parse_wetrakr_datetime(since)
    if current is None or previous is None:
        return True
    return current > previous


# ── Background sync job ───────────────────────────────────────────────────────

async def run_wetrakr_sync(user_id: int, job_id: int) -> None:
    from routers.sync import SyncCancelled, _raise_if_cancelled, _short_error
    print(f"Starting WeTrakr sync for user {user_id}, job {job_id}")
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        stats: dict[str, int] = {
            "movies": 0, "episodes": 0, "ratings": 0, "lists": 0, "list_items": 0,
            "comments": 0, "skipped": 0, "errors": 0,
        }
        try:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.running, processed_items=0, total_items=0, current_step="Pulling from WeTrakr"
                )
            )
            await db.commit()

            result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = result.scalar_one_or_none()

            if not settings or not settings.wetrakr_access_token:
                err = "WeTrakr is not connected"
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                await db.commit()
                return

            try:
                access_token = await ensure_valid_wetrakr_token(db, settings)
            except WeTrakrTokenError as exc:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=_short_error(exc)))
                await db.commit()
                return

            # Nothing changed on WeTrakr since the last pull: skip it entirely.
            # Otherwise every supported feed receives the previous mark as
            # from_date, so a change never causes the full account to be read.
            from_date = settings.wetrakr_last_activity
            activity_stamp: str | None = None
            activities: dict = {}
            try:
                activities = await wetrakr_client.get_last_activities(access_token)
                stamp = activities.get("all")
                visible_until = activities.get("journal_visible_until")
                if isinstance(stamp, str):
                    activity_stamp = (
                        min(stamp, visible_until)
                        if isinstance(visible_until, str)
                        else stamp
                    )
            except Exception as exc:
                logger.warning("WeTrakr last_activities check failed, pulling incrementally anyway: %s", exc)
            if activity_stamp is not None and activity_stamp == from_date:
                print(f"WeTrakr sync job {job_id}: no changes since last pull, skipping")
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed, stats={"unchanged": 1}, processed_items=0,
                ))
                await db.commit()
                return

            _gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
            _gs = _gs_result.scalar_one_or_none()
            api_key = settings.tmdb_api_key or (_gs.tmdb_api_key if _gs else None)

            we_res = await db.execute(select(WatchEvent.media_id).where(WatchEvent.user_id == user_id))
            existing_watched: set[int] = {row[0] for row in we_res}

            # Only the sections WeTrakr says moved since the last pull are
            # read; without a usable stamp every section is read.
            pull_movies = _section_changed(activities, "movies", "last_tracking_watched_at", from_date)
            pull_episodes = _section_changed(activities, "episodes", "last_tracking_watched_at", from_date)
            pull_ratings = _section_changed(activities, "ratings", "all", from_date)
            pull_lists = _section_changed(activities, "lists", "all", from_date)
            pull_comments = (
                _section_changed(activities, "comments", "all", from_date)
                and not wetrakr_client.comments_blocked()
            )

            # ── Watched movies ────────────────────────────────────────────────
            if settings.wetrakr_sync_watched and pull_movies:
                print("  Fetching movie watch history from WeTrakr…")
                history_movies = await wetrakr_client.get_watched_history(access_token, "movies", from_date)
                print(f"  {len(history_movies)} movie plays fetched from WeTrakr")
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(history_movies), current_step="Pulling watched movies"))
                await db.commit()

                for i, item in enumerate(history_movies, start=1):
                    movie_data = item.get("movie") or item
                    tmdb_id = _wetrakr_media_tmdb_id(movie_data)
                    try:
                        if not tmdb_id:
                            stats["skipped"] += 1
                            continue
                        async with db.begin_nested():
                            media = await _get_or_create_movie_media(db, tmdb_id, movie_data.get("title", ""), api_key)
                            if not media:
                                stats["errors"] += 1
                                continue
                            if media.id not in existing_watched:
                                watched_at = None if item.get("watched_at_unknown") else _parse_wetrakr_datetime(item.get("watched_at"))
                                db.add(WatchEvent(
                                    origin="wetrakr",
                                    source="wetrakr",
                                    user_id=user_id,
                                    media_id=media.id,
                                    watched_at=watched_at,
                                    completed=True,
                                    play_count=1,
                                ))
                                existing_watched.add(media.id)
                                stats["movies"] += 1
                            else:
                                stats["skipped"] += 1
                    except Exception as exc:
                        logger.warning("Error processing WeTrakr movie tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1
                    finally:
                        if i % 25 == 0 or i == len(history_movies):
                            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=i))
                            await db.commit()
                            await _raise_if_cancelled(db, job_id)
                await db.commit()

            # ── Watched episodes ──────────────────────────────────────────────
            if settings.wetrakr_sync_watched and pull_episodes:
                print("  Fetching episode watch history from WeTrakr…")
                history_episodes = await wetrakr_client.get_watched_history(access_token, "episodes", from_date)
                print(f"  {len(history_episodes)} episode plays fetched from WeTrakr")

                movies_count = stats["movies"] + stats["skipped"]
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                    total_items=movies_count + len(history_episodes),
                    processed_items=movies_count,
                    current_step="Pulling watched shows",
                ))
                await db.commit()

                plays_by_show: dict[int, list[dict]] = {}
                for entry in history_episodes:
                    ep_data = entry.get("episode") or entry
                    show_data = ep_data.get("show") or {
                        "ids": ep_data.get("show_ids") or {},
                        "title": ep_data.get("show_title", ""),
                    }
                    show_tmdb_id = _wetrakr_media_tmdb_id(show_data)
                    if show_tmdb_id:
                        plays_by_show.setdefault(show_tmdb_id, []).append(entry)
                    else:
                        stats["skipped"] += 1

                processed = movies_count
                for show_tmdb_id, entries in plays_by_show.items():
                    first_ep = entries[0].get("episode") or entries[0]
                    first_show = first_ep.get("show") or {}
                    show_title = first_show.get("title") or first_ep.get("show_title", "")
                    try:
                        async with db.begin_nested():
                            show = await _get_or_create_show(db, show_tmdb_id, show_title, api_key)
                            if not show:
                                stats["errors"] += 1
                                processed += len(entries)
                                continue
                            await db.flush()

                        for entry in entries:
                            ep_data = entry.get("episode") or entry
                            season_num = ep_data.get("season_number")
                            ep_num = ep_data.get("number")
                            if season_num is None or ep_num is None:
                                stats["skipped"] += 1
                                continue
                            try:
                                async with db.begin_nested():
                                    media = await _get_or_create_episode_media(db, show.id, show_tmdb_id, season_num, ep_num, api_key)
                                    if not media:
                                        stats["errors"] += 1
                                        continue
                                    if media.id not in existing_watched:
                                        watched_at = None if entry.get("watched_at_unknown") else _parse_wetrakr_datetime(entry.get("watched_at"))
                                        event = WatchEvent(
                                            origin="wetrakr",
                                            source="wetrakr",
                                            user_id=user_id,
                                            media_id=media.id,
                                            watched_at=watched_at,
                                            completed=True,
                                            play_count=1,
                                        )
                                        db.add(event)
                                        await db.flush()
                                        await record_rewatch_progress(db, user_id, media.id, event.id)
                                        existing_watched.add(media.id)
                                        stats["episodes"] += 1
                                    else:
                                        stats["skipped"] += 1
                            except Exception as exc:
                                logger.warning("Error processing episode s%se%s for show tmdb=%s: %s", season_num, ep_num, show_tmdb_id, exc)
                                stats["errors"] += 1
                    except Exception as exc:
                        logger.warning("Error processing WeTrakr show tmdb=%s: %s", show_tmdb_id, exc)
                        stats["errors"] += 1
                    finally:
                        processed += len(entries)
                        await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=processed))
                        await db.commit()
                        await _raise_if_cancelled(db, job_id)

            # ── Ratings (movies + shows only, see set_ratings_batch) ──────────
            if settings.wetrakr_sync_ratings and pull_ratings:
                print("  Fetching ratings from WeTrakr…")
                rat_res = await db.execute(
                    select(Rating.media_id).where(
                        Rating.user_id == user_id,
                        Rating.season_number.is_(None),
                        Rating.episode_order.is_(None),
                    )
                )
                existing_rated: set[int] = {row[0] for row in rat_res}

                for target, get_media in (
                    ("movies", _get_or_create_movie_media),
                    ("shows", _get_or_create_series_media),
                ):
                    try:
                        items = await wetrakr_client.get_ratings(access_token, target, from_date)
                    except Exception as exc:
                        logger.warning("Failed to fetch WeTrakr %s ratings: %s", target, exc)
                        stats["errors"] += 1
                        continue
                    for item in items:
                        tmdb_id = _wetrakr_media_tmdb_id(item)
                        rating_val, rated_at_raw = _wetrakr_user_rating(item)
                        if not tmdb_id or not rating_val:
                            continue
                        try:
                            async with db.begin_nested():
                                media = await get_media(db, tmdb_id, item.get("title", ""), api_key)
                                if not media:
                                    stats["errors"] += 1
                                    continue
                                if media.id not in existing_rated:
                                    db.add(Rating(
                                        user_id=user_id,
                                        media_id=media.id,
                                        rating=rating_val,
                                        rated_at=_parse_wetrakr_datetime(rated_at_raw) or datetime.utcnow(),
                                    ))
                                    existing_rated.add(media.id)
                                    stats["ratings"] += 1
                        except Exception as exc:
                            logger.warning("Error processing WeTrakr %s rating tmdb=%s: %s", target, tmdb_id, exc)
                            stats["errors"] += 1
                await db.commit()
                await _raise_if_cancelled(db, job_id)

            # ── Lists (movie/show items only, see add_items_to_list) ──────────
            if settings.wetrakr_sync_lists and pull_lists:
                print("  Fetching lists from WeTrakr…")
                try:
                    remote_lists = await wetrakr_client.get_lists(access_token, from_date)
                except Exception as exc:
                    logger.warning("Failed to fetch WeTrakr lists: %s", exc)
                    stats["errors"] += 1
                    remote_lists = []

                for wlist in remote_lists:
                    remote_id = wlist.get("id")
                    if not remote_id:
                        continue
                    try:
                        existing_list_result = await db.execute(
                            select(ListModel).where(ListModel.user_id == user_id, ListModel.wetrakr_list_id == remote_id)
                        )
                        local_list = existing_list_result.scalar_one_or_none()
                        if not local_list:
                            local_list = ListModel(
                                user_id=user_id,
                                name=wlist.get("name") or "WeTrakr list",
                                description=wlist.get("description"),
                                wetrakr_list_id=remote_id,
                            )
                            db.add(local_list)
                            await db.flush()
                            stats["lists"] += 1
                        else:
                            local_list.name = wlist.get("name") or local_list.name
                            local_list.description = wlist.get("description")

                        existing_items_result = await db.execute(
                            select(ListItem.media_id).where(ListItem.list_id == local_list.id, ListItem.season_number.is_(None))
                        )
                        existing_item_ids: set[int] = {row[0] for row in existing_items_result}

                        try:
                            items = await wetrakr_client.get_list_items(access_token, remote_id)
                        except Exception as exc:
                            logger.warning("Could not fetch items for WeTrakr list %s: %s", remote_id, exc)
                            continue

                        for entry in items:
                            item_type = entry.get("type")
                            media: Media | None = None
                            tmdb_id = _wetrakr_media_tmdb_id(entry)
                            if not tmdb_id or item_type not in ("movie", "show"):
                                # season/episode/person list items need
                                # WeTrakr-native id resolution, not built yet.
                                stats["skipped"] += 1
                                continue
                            async with db.begin_nested():
                                if item_type == "movie":
                                    media = await _get_or_create_movie_media(db, tmdb_id, entry.get("title", ""), api_key)
                                else:
                                    media = await _get_or_create_series_media(db, tmdb_id, entry.get("title", ""), api_key)
                            if media and media.id not in existing_item_ids:
                                db.add(ListItem(list_id=local_list.id, media_id=media.id))
                                existing_item_ids.add(media.id)
                                stats["list_items"] += 1
                    except Exception as exc:
                        logger.warning("Error processing WeTrakr list %s: %s", remote_id, exc)
                        stats["errors"] += 1
                await db.commit()
                await _raise_if_cancelled(db, job_id)

            # ── Comments (movies/shows/seasons/episodes, see write_comment) ──
            if settings.wetrakr_sync_comments and pull_comments:
                print("  Fetching comments from WeTrakr…")
                existing_comments_result = await db.execute(select(Comment).where(Comment.user_id == user_id))
                existing_comments = existing_comments_result.scalars().all()
                existing_comment_keys = {
                    (c.media_type, c.tmdb_id, c.season_number, c.episode_number, c.content)
                    for c in existing_comments
                }
                # Comments pulled before the platform credit existed have no source yet.
                unsourced_by_wetrakr_id = {
                    c.wetrakr_comment_id: c for c in existing_comments
                    if c.wetrakr_comment_id and not c.wetrakr_source
                }
                lookup_caches: dict[str, dict[int, dict]] = {}
                for target in ("movies", "shows", "seasons", "episodes"):
                    try:
                        entries = await wetrakr_client.get_comments(access_token, target, from_date)
                    except wetrakr_client.WeTrakrCommentsDisabled:
                        logger.info("WeTrakr comments are not enabled for this app; skipping comment pull")
                        break
                    except Exception as exc:
                        logger.warning("Failed to fetch WeTrakr %s comments: %s", target, exc)
                        stats["errors"] += 1
                        continue
                    for entry in entries:
                        if (unsourced := unsourced_by_wetrakr_id.get(entry.get("id"))) is not None:
                            unsourced.wetrakr_source = (entry.get("source") or "wetrakr")[:32]
                        try:
                            resolved = await _resolve_wetrakr_comment_target(entry, lookup_caches)
                        except Exception as exc:
                            # Transient: leave the stamp alone so the next
                            # pull picks this comment up again.
                            logger.warning("Could not resolve a WeTrakr %s comment: %s", target, exc)
                            stats["errors"] += 1
                            continue
                        if not resolved:
                            stats["skipped"] += 1
                            continue
                        media_type, tmdb_id, season_number, episode_number = resolved
                        content = entry.get("text") or ""
                        key = (media_type, tmdb_id, season_number, episode_number, content)
                        if key in existing_comment_keys:
                            stats["skipped"] += 1
                            continue
                        try:
                            async with db.begin_nested():
                                db.add(Comment(
                                    user_id=user_id,
                                    media_type=media_type,
                                    tmdb_id=tmdb_id,
                                    season_number=season_number,
                                    episode_number=episode_number,
                                    content=content,
                                    is_spoiler=bool(entry.get("spoiler")),
                                    created_at=_parse_wetrakr_datetime(entry.get("comment_added_at")) or datetime.utcnow(),
                                    wetrakr_comment_id=entry.get("id"),
                                    wetrakr_source=(entry.get("source") or "wetrakr")[:32],
                                ))
                            existing_comment_keys.add(key)
                            stats["comments"] += 1
                        except Exception as exc:
                            logger.warning("Error saving WeTrakr comment (tmdb=%s): %s", tmdb_id, exc)
                            stats["errors"] += 1
                await db.commit()
                await _raise_if_cancelled(db, job_id)

            print(
                f"WeTrakr sync job {job_id} completed. "
                f"Movies: {stats['movies']} new. Episodes: {stats['episodes']} new. "
                f"Ratings: {stats['ratings']} new. Lists: {stats['lists']} new, {stats['list_items']} items. "
                f"Comments: {stats['comments']} new. Skipped: {stats['skipped']}. Errors: {stats['errors']}."
            )
            if (
                activity_stamp is not None
                and stats["errors"] == 0
                and (from_date is None or activity_stamp >= from_date)
            ):
                settings.wetrakr_last_activity = activity_stamp
            # A pull only populates scrob's own data — never auto-pushed elsewhere.
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats=stats,
                    processed_items=stats["movies"] + stats["episodes"] + stats["ratings"] + stats["list_items"] + stats["comments"],
                )
            )
            await db.commit()

        except SyncCancelled:
            print(f"WeTrakr sync job {job_id} cancelled")
            await db.rollback()  # session may be poisoned by a failed flush — see the Exception branch below
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.cancelled, stats=stats))
            await db.commit()

        except Exception as exc:
            print(f"WeTrakr sync job {job_id} failed: {exc}")
            # A DB error mid-loop leaves the session unusable for any further
            # query until rolled back (PendingRollbackError) — without this,
            # marking the job failed here would itself raise and the job
            # would stay stuck "running" forever (#comments int32 incident).
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=_short_error(exc)))
            await db.commit()


@router.post("/sync")
async def sync_wetrakr(
    background_tasks: BackgroundTasks,
    full: bool = True,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if not settings or not settings.wetrakr_access_token:
        raise HTTPException(status_code=400, detail="WeTrakr is not connected")

    _tmdb_key = settings.tmdb_api_key if settings else None
    if not _tmdb_key:
        _gs_r = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        _gs = _gs_r.scalar_one_or_none()
        _tmdb_key = _gs.tmdb_api_key if _gs else None
    if not _tmdb_key:
        raise HTTPException(status_code=400, detail="TMDB API key required for sync")

    await _claim_job_slot(db, current_user.id)

    if full:
        # A button press reconciles everything (a compact full read is a
        # handful of requests); only the scheduler pulls incrementally.
        settings.wetrakr_last_activity = None

    job = SyncJob(user_id=current_user.id, source=CollectionSource.wetrakr, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_wetrakr_sync, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "WeTrakr sync is running in the background"}


# ── Push (Scrob → WeTrakr) ─────────────────────────────────────────────────────

async def push_live_changes(
    user_id: int,
    watched_ids: set[int] | list[int],
    watched_at_by_media: dict[int, datetime | None],
    ratings: dict[int, float] | None = None,
    allow_rewatch: bool = False,
) -> None:
    """Push a handful of fresh changes (a play just finished, a title just
    marked watched, a rating just set) to WeTrakr straight away.

    allow_rewatch is true only when the caller knows this is a real new play
    (a webhook finish or a manual mark); WeTrakr still collapses one within
    the hour of an existing play, and an undated play is never a rewatch.
    Best-effort: callers log failures, the scheduled push is the safety net.
    """
    ratings = ratings or {}
    media_ids = set(watched_ids) | set(ratings)
    if not media_ids:
        return
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))).scalar_one_or_none()
        if not settings or not settings.wetrakr_access_token:
            return
        push_watched = bool(settings.wetrakr_push_watched and watched_ids)
        push_ratings = bool(settings.wetrakr_push_ratings and ratings)
        if not (push_watched or push_ratings):
            return
        access_token = await ensure_valid_wetrakr_token(db, settings)

        media_by_id = {m.id: m for m in (await db.execute(select(Media).where(Media.id.in_(media_ids)))).scalars().all()}
        show_ids = {m.show_id for m in media_by_id.values() if m.show_id}
        shows_by_id: dict[int, Show] = {}
        if show_ids:
            shows_by_id = {s.id: s for s in (await db.execute(select(Show).where(Show.id.in_(show_ids)))).scalars().all()}

        if push_watched:
            dated_movies, undated_movies = [], []
            dated_eps, undated_eps = [], []
            for mid in watched_ids:
                media = media_by_id.get(mid)
                if not media or not media.tmdb_id or is_unmapped_tvdb_episode(media):
                    continue
                watched_at = watched_at_by_media.get(mid)
                if media.media_type == MediaType.movie:
                    (dated_movies if watched_at else undated_movies).append((media.tmdb_id, watched_at))
                elif media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
                    show = shows_by_id.get(media.show_id)
                    if show and show.tmdb_id:
                        (dated_eps if watched_at else undated_eps).append((show.tmdb_id, media.season_number, media.episode_number, watched_at))
            # A sync can surface far more than one write takes, so go through
            # the same chunking as the scheduled push.
            dated = [("movie", m) for m in dated_movies] + [("episode", e) for e in dated_eps]
            undated = [("movie", m) for m in undated_movies] + [("episode", e) for e in undated_eps]
            if dated:
                await _send_in_chunks(dated, _watched_sender(access_token, allow_rewatch))
            if undated:
                await _send_in_chunks(undated, _watched_sender(access_token, False))

        if push_ratings:
            movie_ratings, show_ratings = [], []
            for mid, rating in ratings.items():
                media = media_by_id.get(mid)
                if not media or not media.tmdb_id:
                    continue
                if media.media_type == MediaType.movie:
                    movie_ratings.append((media.tmdb_id, rating))
                elif media.media_type == MediaType.series:
                    show_ratings.append((media.tmdb_id, rating))
            rated = [("movie", r) for r in movie_ratings] + [("show", r) for r in show_ratings]
            if rated:
                await _send_in_chunks(rated, _ratings_sender(access_token))


async def _run_wetrakr_push(user_id: int, job_id: int) -> None:
    from routers.sync import SyncCancelled, _raise_if_cancelled, _select_in_chunks, _short_error, _latest_watched_at
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        succeeded = 0
        failed = 0
        # Failures of the watched/ratings batches decide whether the push
        # bookmark moves. A list or comment failing must not make the next run
        # resend the whole history, so those are tracked on their own.
        core_failed = 0
        lists_failed = 0
        quota_hit = False
        try:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running))
            await db.commit()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            if not settings or not settings.wetrakr_access_token:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message="WeTrakr is not connected"))
                await db.commit()
                return

            try:
                access_token = await ensure_valid_wetrakr_token(db, settings)
            except WeTrakrTokenError as exc:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=_short_error(exc)))
                await db.commit()
                return

            # Incremental: after the first (full) push only what is newer than
            # the bookmark is sent. The bookmark is taken before reading so
            # an event written mid-push is picked up by the next run.
            push_started_at = datetime.utcnow()
            since = settings.wetrakr_last_push_at

            all_media_ids: set[int] = set()
            watched_ids: set[int] = set()
            ratings_map: dict[int, float] = {}

            if settings.wetrakr_push_watched:
                watched_query = select(WatchEvent.media_id).where(
                    WatchEvent.user_id == user_id,
                    # Plays pulled from WeTrakr are never pushed back to it.
                    WatchEvent.origin.is_distinct_from("wetrakr"),
                )
                if since is not None:
                    watched_query = watched_query.where(WatchEvent.created_at > since)
                watched_result = await db.execute(watched_query.distinct())
                watched_ids = {row[0] for row in watched_result.all()}
                all_media_ids |= watched_ids
                watched_at_by_media = await _latest_watched_at(db, user_id, list(watched_ids))

            if settings.wetrakr_push_ratings:
                ratings_query = select(Rating.media_id, Rating.rating).where(
                    Rating.user_id == user_id,
                    Rating.rating.isnot(None),
                    Rating.season_number.is_(None),
                    Rating.episode_order.is_(None),
                )
                if since is not None:
                    ratings_query = ratings_query.where(Rating.rated_at > since)
                ratings_result = await db.execute(ratings_query)
                ratings_map = {row[0]: row[1] for row in ratings_result.all()}
                all_media_ids |= set(ratings_map.keys())

            if not all_media_ids and not settings.wetrakr_push_lists and not settings.wetrakr_push_comments:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats={"succeeded": 0, "failed": 0}, processed_items=0, total_items=0))
                await db.commit()
                return

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(all_media_ids)))
            await db.commit()

            media_rows = await _select_in_chunks(db, lambda chunk: select(Media).where(Media.id.in_(chunk)), list(all_media_ids))
            media_by_id: dict[int, Media] = {m.id: m for m in media_rows}

            show_ids = {m.show_id for m in media_by_id.values() if m.show_id}
            shows_by_id: dict[int, Show] = {}
            if show_ids:
                show_rows = await _select_in_chunks(db, lambda chunk: select(Show).where(Show.id.in_(chunk)), list(show_ids))
                shows_by_id = {s.id: s for s in show_rows}

            movie_candidates: list[tuple[int, datetime]] = []
            episode_candidates: list[tuple[int, int, int, datetime]] = []

            if settings.wetrakr_push_watched:
                for mid in watched_ids:
                    media = media_by_id.get(mid)
                    if not media or not media.tmdb_id or is_unmapped_tvdb_episode(media):
                        continue
                    watched_at = watched_at_by_media.get(mid)
                    if media.media_type == MediaType.movie:
                        movie_candidates.append((media.tmdb_id, watched_at))
                    elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                        show = shows_by_id.get(media.show_id)
                        if show and show.tmdb_id:
                            episode_candidates.append((show.tmdb_id, media.season_number, media.episode_number, watched_at))
                movie_candidates.sort(key=lambda item: item[1] or datetime.min)
                episode_candidates.sort(key=lambda item: item[3] or datetime.min)

            movie_rating_candidates: list[tuple[int, float]] = []
            show_rating_candidates: list[tuple[int, float]] = []

            if settings.wetrakr_push_ratings:
                for mid, rating in ratings_map.items():
                    media = media_by_id.get(mid)
                    if not media or not media.tmdb_id or is_unmapped_tvdb_episode(media):
                        continue
                    if media.media_type == MediaType.movie:
                        movie_rating_candidates.append((media.tmdb_id, rating))
                    elif media.media_type == MediaType.series:
                        show_rating_candidates.append((media.tmdb_id, rating))
                    # Episode/season ratings have no push path yet — see
                    # core/wetrakr.py: set_ratings_batch.

            push_tasks: list[tuple[str, int, Callable[[], Awaitable[None]]]] = []

            # allow_rewatch stays false here: this job re-reads local history,
            # so a play it already sent (or one pushed live) must not become a
            # second one. Genuine rewatches go out through push_live_changes.
            history_pending: list[tuple[str, tuple]] = [("movie", item) for item in movie_candidates]
            history_pending.extend(("episode", item) for item in episode_candidates)
            watched_send = _watched_sender(access_token)
            for i in range(0, len(history_pending), WETRAKR_PUSH_BATCH_SIZE):
                chunk = history_pending[i:i + WETRAKR_PUSH_BATCH_SIZE]
                push_tasks.append(("watched", len(chunk), functools.partial(_send_chunk, watched_send, chunk)))

            rating_pending: list[tuple[str, tuple]] = [("movie", item) for item in movie_rating_candidates]
            rating_pending.extend(("show", item) for item in show_rating_candidates)
            ratings_send = _ratings_sender(access_token)
            for i in range(0, len(rating_pending), WETRAKR_PUSH_BATCH_SIZE):
                chunk = rating_pending[i:i + WETRAKR_PUSH_BATCH_SIZE]
                push_tasks.append(("ratings", len(chunk), functools.partial(_send_chunk, ratings_send, chunk)))

            total = sum(item_count for _, item_count, _ in push_tasks)

            if push_tasks:
                print(f"WeTrakr full push: pushing {total} items in {len(push_tasks)} batch requests…")
                # One request at a time (core/wetrakr.py also serialises per
                # user and honours Retry-After on a 429).
                for category, item_count, task in push_tasks:
                    try:
                        await task()
                        succeeded += item_count
                    except wetrakr_client.WeTrakrQuotaExceeded as exc:
                        # Every further request would fail the same way.
                        failed += item_count
                        core_failed += item_count
                        quota_hit = True
                        logger.warning("WeTrakr push stopped (%s): %s", category, exc)
                        break
                    except Exception as exc:
                        failed += item_count
                        core_failed += item_count
                        logger.warning("WeTrakr push batch failed (%s, %d items): %s", category, item_count, exc)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=succeeded + failed))
                    await db.commit()
                    await _raise_if_cancelled(db, job_id)
                print(f"WeTrakr full push: {succeeded}/{total} succeeded")

            # ── Lists (movie/show items only — see add_items_to_list) ────────
            # Idempotent server-side, so safe to re-run: an already-linked
            # list's items are just re-added (a no-op for ones already there).
            lists_since = settings.wetrakr_last_lists_push_at
            lists_pushed = False
            local_lists: list = []
            if settings.wetrakr_push_lists and not quota_hit:
                lists_pushed = True
                lists_result = await db.execute(select(ListModel).where(ListModel.user_id == user_id))
                local_lists = lists_result.scalars().all()
                if lists_since is not None:
                    # Only lists that are new or gained items since the last
                    # list push (its own bookmark: a failing list must not
                    # hold back the watched/ratings bookmark, nor the reverse).
                    changed_q = await db.execute(
                        select(ListItem.list_id).where(ListItem.added_at > lists_since).distinct()
                    )
                    changed_list_ids = {row[0] for row in changed_q.all()}
                    local_lists = [
                        lst for lst in local_lists
                        if not lst.wetrakr_list_id or lst.id in changed_list_ids
                    ]
                if local_lists:
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(all_media_ids) + len(local_lists)))
                    await db.commit()
                for local_list in local_lists:
                    try:
                        remote_id = local_list.wetrakr_list_id
                        if not remote_id:
                            created = await wetrakr_client.create_list(access_token, local_list.name, local_list.description)
                            remote_id = created.get("id")
                            if not remote_id:
                                raise ValueError("WeTrakr did not return a list id")
                            local_list.wetrakr_list_id = remote_id
                            await db.commit()

                        items_result = await db.execute(
                            select(Media.tmdb_id, Media.media_type)
                            .join(ListItem, ListItem.media_id == Media.id)
                            .where(ListItem.list_id == local_list.id, ListItem.season_number.is_(None))
                        )
                        movie_tmdb_ids: list[int] = []
                        show_tmdb_ids: list[int] = []
                        for tmdb_id, media_type in items_result.all():
                            if not tmdb_id:
                                continue
                            if media_type == MediaType.movie:
                                movie_tmdb_ids.append(tmdb_id)
                            elif media_type == MediaType.series:
                                show_tmdb_ids.append(tmdb_id)
                        if movie_tmdb_ids or show_tmdb_ids:
                            await wetrakr_client.add_items_to_list(access_token, remote_id, movie_tmdb_ids, show_tmdb_ids)
                        succeeded += 1
                    except wetrakr_client.WeTrakrQuotaExceeded as exc:
                        failed += 1
                        lists_failed += 1
                        quota_hit = True
                        logger.warning("WeTrakr list push stopped: %s", exc)
                        break
                    except Exception as exc:
                        failed += 1
                        lists_failed += 1
                        logger.warning("WeTrakr list push failed for list %s: %s", local_list.id, exc)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=succeeded + failed))
                    await db.commit()
                    await _raise_if_cancelled(db, job_id)

            # ── Comments (movie/show only — see write_comment) ───────────────
            # Only ever considers comments with no wetrakr_comment_id yet:
            # WeTrakr has no edit/upsert for comments, so anything already
            # linked (pulled in, or pushed by an earlier run) is left alone.
            if settings.wetrakr_push_comments and not quota_hit and not wetrakr_client.comments_blocked():
                comments_result = await db.execute(
                    select(Comment).where(
                        Comment.user_id == user_id,
                        Comment.wetrakr_comment_id.is_(None),
                        Comment.tmdb_id.isnot(None),
                        Comment.media_type.in_(["movie", "series"]),
                        Comment.season_number.is_(None),
                    )
                )
                pending_comments = comments_result.scalars().all()
                if pending_comments:
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                        total_items=len(all_media_ids) + (len(local_lists) if settings.wetrakr_push_lists else 0) + len(pending_comments)
                    ))
                    await db.commit()
                consecutive_failures = 0
                for comment in pending_comments:
                    try:
                        created = await wetrakr_client.write_comment(
                            access_token,
                            movie_tmdb_id=comment.tmdb_id if comment.media_type == "movie" else None,
                            show_tmdb_id=comment.tmdb_id if comment.media_type == "series" else None,
                            text=comment.content,
                            spoiler=comment.is_spoiler,
                        )
                        comment.wetrakr_comment_id = created.get("id")
                        comment.wetrakr_source = (created.get("source") or "wetrakr")[:32]
                        await db.commit()
                        succeeded += 1
                        consecutive_failures = 0
                    except wetrakr_client.WeTrakrCommentsDisabled:
                        logger.info("WeTrakr comments are not enabled for this app; skipping comment push")
                        break
                    except wetrakr_client.WeTrakrQuotaExceeded as exc:
                        failed += 1
                        quota_hit = True
                        logger.warning("WeTrakr comment push stopped: %s", exc)
                        break
                    except Exception as exc:
                        failed += 1
                        consecutive_failures += 1
                        logger.warning("WeTrakr comment push failed for comment %s: %s", comment.id, exc)
                        if consecutive_failures >= 3:
                            # Unlinked comments are retried every run, one
                            # request each: don't keep hammering when the
                            # failures are not about one odd comment.
                            logger.warning("WeTrakr comment push stopped after %d failures in a row", consecutive_failures)
                            break
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=succeeded + failed))
                    await db.commit()
                    await _raise_if_cancelled(db, job_id)

            if core_failed == 0:
                settings.wetrakr_last_push_at = push_started_at
            if lists_pushed and lists_failed == 0 and not quota_hit:
                settings.wetrakr_last_lists_push_at = push_started_at
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats={"succeeded": succeeded, "failed": failed},
                    processed_items=succeeded + failed,
                )
            )
            await db.commit()

        except SyncCancelled:
            print(f"WeTrakr push job {job_id} cancelled")
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                status=SyncStatus.cancelled,
                stats={"succeeded": succeeded, "failed": failed},
                processed_items=succeeded + failed,
            ))
            await db.commit()

        except Exception as exc:
            print(f"WeTrakr push job {job_id} failed: {exc}")
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=_short_error(exc)))
            await db.commit()


@router.post("/push")
async def push_wetrakr(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    if not settings or not settings.wetrakr_access_token:
        raise HTTPException(status_code=400, detail="WeTrakr is not connected")
    if not any([settings.wetrakr_push_watched, settings.wetrakr_push_ratings, settings.wetrakr_push_lists, settings.wetrakr_push_comments]):
        raise HTTPException(status_code=400, detail="Enable 'Scrob → WeTrakr' push flags first")
    await _claim_job_slot(db, current_user.id)
    job = SyncJob(user_id=current_user.id, source=CollectionSource.wetrakr, status=SyncStatus.pending, job_type="push")
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(_run_wetrakr_push, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "WeTrakr push is running in the background"}
