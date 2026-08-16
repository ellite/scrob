"""Episode calendar: recently aired and upcoming episodes for the user's
shows (#194).

Built from one TMDB base-details call per followed, still-running show
(next/last_episode_to_air), which is why the result is cached whole in
user_calendar_cache and recomputed at most once per TTL or on explicit
refresh. `cached_only=true` (used by pages that must render instantly) never
waits on TMDB: it serves whatever cache exists and warms it in the
background otherwise.
"""

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import get_db
from dependencies import get_current_user_or_api_key
from models.base import MediaType
from models.calendar_cache import UserCalendarCache
from models.collection import Collection
from models.media import Media
from models.show import Show
from models.users import User

router = APIRouter()

CALENDAR_TTL = timedelta(hours=24)
CALENDAR_SCHEMA = 1
AIRED_LOOKBACK_DAYS = 14
FETCH_CONCURRENCY = 8

_computing: set[int] = set()


async def compute_calendar(db: AsyncSession, user_id: int) -> dict:
    """Recently aired and upcoming episodes for the user's collected shows."""
    from core import tmdb as tmdb_client
    from routers.media import check_tmdb_key, get_user_tmdb_key

    episode_show_ids = (
        select(Media.show_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == user_id,
            Media.show_id.isnot(None),
            Media.media_type == MediaType.episode,
        )
        .distinct()
    )
    direct_series_tmdb_ids = (
        select(Media.tmdb_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == user_id,
            Media.media_type == MediaType.series,
            Media.tmdb_id.isnot(None),
        )
        .distinct()
    )
    shows = (
        await db.execute(
            select(Show).where(
                or_(
                    Show.id.in_(episode_show_ids),
                    Show.tmdb_id.in_(direct_series_tmdb_ids),
                )
            )
        )
    ).scalars().all()

    candidates = [
        s for s in shows
        if s.tmdb_id and (s.status or "") not in ("Ended", "Canceled")
    ]

    now = datetime.utcnow()
    entries: list[dict] = []
    api_key = await get_user_tmdb_key(db, user_id)
    if check_tmdb_key(api_key) and candidates:
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _fetch(s: Show):
            async with sem:
                try:
                    d = await tmdb_client.get_show_light(s.tmdb_id, api_key=api_key)
                except Exception:
                    return
                for kind, ep in (("aired", d.get("last_episode_to_air")), ("upcoming", d.get("next_episode_to_air"))):
                    if not ep or not ep.get("air_date"):
                        continue
                    entries.append({
                        "air_date": ep["air_date"],
                        "kind": kind,
                        "show_tmdb_id": s.tmdb_id,
                        "show_tvdb_id": s.tvdb_id,
                        "show_title": s.title,
                        "poster_path": s.poster_path,
                        "season": ep.get("season_number"),
                        "episode": ep.get("episode_number"),
                        "episode_name": ep.get("name"),
                    })

        await asyncio.gather(*(_fetch(s) for s in candidates))

    today = now.date().isoformat()
    aired_cutoff = (now - timedelta(days=AIRED_LOOKBACK_DAYS)).date().isoformat()
    entries = [
        e for e in entries
        if (e["kind"] == "upcoming" and e["air_date"] >= today)
        or (e["kind"] == "aired" and aired_cutoff <= e["air_date"])
    ]
    entries.sort(key=lambda e: (e["air_date"], e["show_title"] or ""))
    return {
        "schema": CALENDAR_SCHEMA,
        "generated_at": now.isoformat(),
        "shows_checked": len(candidates),
        "entries": entries,
    }


async def _load_or_compute(db: AsyncSession, user_id: int, force: bool) -> dict:
    row = (
        await db.execute(select(UserCalendarCache).where(UserCalendarCache.user_id == user_id))
    ).scalars().first()
    if (
        row and not force
        and (datetime.utcnow() - row.computed_at) < CALENDAR_TTL
        and (row.payload or {}).get("schema") == CALENDAR_SCHEMA
    ):
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
        if (
            row
            and (datetime.utcnow() - row.computed_at) < CALENDAR_TTL
            and (row.payload or {}).get("schema") == CALENDAR_SCHEMA
        ):
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
