"""Episode calendar: real per-day schedule for the next 14 days, for shows
the user is collecting or watching (#194).

The original attempt at this (#243) built the calendar from each show's
next_episode_to_air/last_episode_to_air - single TMDB pointers, not a real
per-day list, so a multi-episode drop only ever showed one entry and the
calendar had gaps. This version fetches the actual season episode list (with
a real air_date per episode) for each candidate show's currently-airing
season and filters that to the target window instead.

Building it costs two TMDB calls per followed, still-running show (show
details for next_episode_to_air, then that season's full episode list), so
the computed payload is cached whole in user_calendar_cache and recomputed at
most once per TTL or on explicit refresh. cached_only=true (used by pages
that must render instantly) never waits on TMDB: it serves whatever cache
exists and warms it in the background otherwise.
"""

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import settings
from core.episode_order import get_order_keys_for_series, get_positions_for_series
from db import get_db
from dependencies import get_current_user_or_api_key
from models.base import MediaType
from models.calendar_cache import UserCalendarCache
from models.collection import Collection
from models.events import WatchEvent
from models.media import Media
from models.show import Show
from models.users import User, UserSettings

router = APIRouter()

CALENDAR_TTL = timedelta(hours=24)
CALENDAR_SCHEMA = 4
CALENDAR_WINDOW_DAYS = 14
FETCH_CONCURRENCY = 8

_computing: set[int] = set()


def _server_today() -> date:
    """Server-configured TZ (same reference used everywhere else that syncs/
    dates against a single instance-wide clock) - not a per-browser value. A
    naive UTC boundary would put "today" off by a day near midnight in the
    server's real timezone."""
    try:
        tz = ZoneInfo(settings.tz)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


async def _candidate_shows(db: AsyncSession, user_id: int) -> list[Show]:
    """Shows the user is either collecting or has watched at least one
    episode of - the "no Plex connected" case still needs to show up via
    watch history alone (Trakt/manual import/etc.), not just synced library
    collection."""
    collected_show_ids = (
        select(Media.show_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == user_id, Media.show_id.isnot(None), Media.media_type == MediaType.episode)
        .distinct()
    )
    watched_show_ids = (
        select(Media.show_id)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == user_id, WatchEvent.completed == True,
            Media.show_id.isnot(None), Media.media_type == MediaType.episode,
        )
        .distinct()
    )
    # A show added to a list as a whole (no episodes collected/watched yet)
    # still has its own series-type Media row if it was ever added that way.
    direct_series_tmdb_ids = (
        select(Media.tmdb_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == user_id, Media.media_type == MediaType.series, Media.tmdb_id.isnot(None))
        .distinct()
    )
    shows = (await db.execute(
        select(Show).where(or_(
            Show.id.in_(collected_show_ids),
            Show.id.in_(watched_show_ids),
            Show.tmdb_id.in_(direct_series_tmdb_ids),
        ))
    )).scalars().all()

    user_settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    user_settings = user_settings_result.scalar_one_or_none()
    dropped_show_ids = set(user_settings.dropped_shows or []) if user_settings else set()

    return [
        s for s in shows
        if s.tmdb_id and (s.status or "") not in ("Ended", "Canceled") and s.id not in dropped_show_ids
    ]


async def compute_calendar(db: AsyncSession, user_id: int) -> dict:
    from core import tmdb as tmdb_client
    from core.translations import get_user_metadata_language
    from routers.media import check_tmdb_key, get_user_tmdb_key

    # Handed back in the payload too, so the frontend's Today/Yesterday/
    # Tomorrow labels use this same reference instead of the viewer's own
    # browser clock.
    today = _server_today()

    candidates = await _candidate_shows(db, user_id)

    api_key = await get_user_tmdb_key(db, user_id)
    if not check_tmdb_key(api_key) or not candidates:
        return {
            "schema": CALENDAR_SCHEMA, "generated_at": datetime.utcnow().isoformat(),
            "today": today.isoformat(), "shows_checked": 0, "entries": [],
        }

    language = await get_user_metadata_language(db, user_id)
    window_start = today - timedelta(days=1)
    window_end = today + timedelta(days=CALENDAR_WINDOW_DAYS - 1)
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _fetch(show: Show) -> list[dict]:
        async with sem:
            try:
                detail = await tmdb_client.get_show_light(show.tmdb_id, api_key=api_key, language=language)
            except Exception:
                return []
            season_numbers: set[int] = set()
            next_ep = detail.get("next_episode_to_air")
            if next_ep and next_ep.get("season_number") is not None:
                season_numbers.add(next_ep["season_number"])
            # Specials (season 0) never surface as next_episode_to_air, so pull
            # that season explicitly whenever the show has one and scan it for
            # anything landing in the window too (#333).
            if any((s or {}).get("season_number") == 0 for s in detail.get("seasons") or []):
                season_numbers.add(0)
            if not season_numbers:
                return []
            seasons: list[dict] = []
            for sn in season_numbers:
                try:
                    seasons.append(await tmdb_client.get_season(show.tmdb_id, sn, api_key=api_key, language=language))
                except Exception:
                    continue

        out = []
        for season in seasons:
            for ep in season.get("episodes") or []:
                air_date = ep.get("air_date")
                if not air_date or not (window_start.isoformat() <= air_date <= window_end.isoformat()):
                    continue
                out.append({
                    "air_date": air_date,
                    "show_id": show.id,
                    "show_tmdb_id": show.tmdb_id,
                    "show_tvdb_id": show.tvdb_id,
                    "show_title": show.title,
                    "poster_path": show.poster_path,
                    "season_number": ep.get("season_number"),
                    "episode_number": ep.get("episode_number"),
                    "episode_name": ep.get("name"),
                })
        return out

    fetched = await asyncio.gather(*(_fetch(s) for s in candidates))
    entries = [e for group in fetched for e in group]

    # Batch-resolve collected/watched status against local Media rows - an
    # episode with no local row at all just hasn't been scanned/aired into
    # the library yet, which is the normal case for anything upcoming.
    show_ids = {e["show_id"] for e in entries}
    if show_ids:
        media_rows = (await db.execute(
            select(Media.show_id, Media.season_number, Media.episode_number, Media.id)
            .where(Media.show_id.in_(show_ids), Media.media_type == MediaType.episode)
        )).all()
        media_by_key = {(sid, sn, en): mid for sid, sn, en, mid in media_rows}
        media_ids = list(media_by_key.values())
        collected_ids: set[int] = set()
        watched_ids: set[int] = set()
        if media_ids:
            collected_ids = {
                r[0] for r in (await db.execute(
                    select(Collection.media_id).where(Collection.media_id.in_(media_ids), Collection.user_id == user_id)
                )).all()
            }
            watched_ids = {
                r[0] for r in (await db.execute(
                    select(WatchEvent.media_id).where(
                        WatchEvent.media_id.in_(media_ids), WatchEvent.user_id == user_id, WatchEvent.completed == True,
                    )
                )).all()
            }
        for e in entries:
            media_id = media_by_key.get((e["show_id"], e["season_number"], e["episode_number"]))
            e["collected"] = media_id in collected_ids if media_id else False
            e["watched"] = media_id in watched_ids if media_id else False
            del e["show_id"]

    # #174: render each entry in the ordering the user picked for that show.
    order_keys = await get_order_keys_for_series(
        db, user_id, list({e["show_tmdb_id"] for e in entries if e.get("show_tmdb_id")})
    )
    if order_keys:
        by_pair = await get_positions_for_series(
            db, [(sid, key) for sid, key in order_keys.items()]
        )
        flat: dict[tuple[int, int, int], object] = {}
        for (sid, _key), canon_map in by_pair.items():
            for (cs, ce), pos in canon_map.items():
                flat[(sid, cs, ce)] = pos
        for e in entries:
            key = order_keys.get(e.get("show_tmdb_id"))
            if not key:
                continue
            e["show_episode_order"] = key
            pos = flat.get((e["show_tmdb_id"], e["season_number"], e["episode_number"]))
            if pos:
                e["display_season_number"] = pos.display_season
                e["display_episode_number"] = pos.display_episode

    entries.sort(key=lambda e: (
        e["air_date"], e["show_title"] or "",
        e["season_number"] if e["season_number"] is not None else 0,
        e["episode_number"] if e["episode_number"] is not None else 0,
    ))
    return {
        "schema": CALENDAR_SCHEMA,
        "generated_at": datetime.utcnow().isoformat(),
        "today": today.isoformat(),
        "shows_checked": len(candidates),
        "entries": entries,
    }


def _is_cache_fresh(row: UserCalendarCache | None) -> bool:
    if not row or (datetime.utcnow() - row.computed_at) >= CALENDAR_TTL:
        return False
    payload = row.payload or {}
    if payload.get("schema") != CALENDAR_SCHEMA:
        return False
    # A cache built before local midnight is stale the instant the calendar
    # day rolls over, even if it's still within the raw TTL - 24h is a
    # rolling duration, not a "still the same calendar day" guarantee, so a
    # payload computed at 17:20 yesterday would otherwise still read as
    # fresh this morning with every Today/Yesterday/Tomorrow label (and the
    # airing-today widget's "today" filter) pointing at yesterday.
    return payload.get("today") == _server_today().isoformat()


def _is_cache_usable(
    row: UserCalendarCache | None, max_age: timedelta = timedelta(hours=48)
) -> bool:
    """A cache row good enough to show immediately even when _is_cache_fresh
    rejected it for the calendar-day rollover.

    Any payload computed in the last couple of days still has a 14-day forward
    window that covers today, so the airing-today widget can serve it right
    away and recompute in the background instead of blocking its first load
    after local midnight on a full TMDB fan-out (#194). Deliberately ignores
    the day-match and the 24h TTL - staleness within max_age is the point.
    """
    if not row or (datetime.utcnow() - row.computed_at) >= max_age:
        return False
    return (row.payload or {}).get("schema") == CALENDAR_SCHEMA


async def _load_or_compute(db: AsyncSession, user_id: int, force: bool) -> dict:
    row = (
        await db.execute(select(UserCalendarCache).where(UserCalendarCache.user_id == user_id))
    ).scalars().first()
    if not force and _is_cache_fresh(row):
        return {"computed_at": row.computed_at.isoformat(), "cached": True, "calendar": row.payload}
    payload = await compute_calendar(db, user_id)
    if row:
        row.payload = payload
        row.computed_at = datetime.utcnow()
    else:
        row = UserCalendarCache(user_id=user_id, payload=payload, computed_at=datetime.utcnow())
        db.add(row)
    await db.commit()
    return {"computed_at": row.computed_at.isoformat(), "cached": False, "calendar": payload}


async def _background_compute(user_id: int) -> None:
    """Warm the calendar cache without blocking the caller."""
    if user_id in _computing:
        return
    _computing.add(user_id)
    try:
        from db import engine

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as bg_db:
            await _load_or_compute(bg_db, user_id, force=False)
    except Exception as e:
        print(f"Calendar background compute failed: {e}")
    finally:
        _computing.discard(user_id)


@router.get("")
async def get_calendar(
    cached_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    if cached_only:
        row = (
            await db.execute(select(UserCalendarCache).where(UserCalendarCache.user_id == current_user.id))
        ).scalars().first()
        if _is_cache_fresh(row):
            return {"computed_at": row.computed_at.isoformat(), "cached": True, "calendar": row.payload}
        asyncio.create_task(_background_compute(current_user.id))
        return {"computed_at": None, "cached": False, "calendar": {"entries": []}}
    return await _load_or_compute(db, current_user.id, force=False)


@router.post("/refresh")
async def refresh_calendar(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    return await _load_or_compute(db, current_user.id, force=True)
