from collections import defaultdict
from datetime import date as DateType, timedelta as TimeDelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from dependencies import get_optional_user_or_api_key
from models.events import WatchEvent
from models.media import Media
from models.show import Show as ShowModel
from models.users import User
from core.watch_stats import effective_runtime_expression
from .common import _check_profile_access

router = APIRouter()


def _watch_date_filters(since: Optional[DateType], until: Optional[DateType]) -> list:
    date_filters = [WatchEvent.completed == True]
    if since:
        date_filters.append(WatchEvent.watched_at >= since)
    if until:
        date_filters.append(WatchEvent.watched_at < until + TimeDelta(days=1))
    return date_filters


def _rank_top_media(top_rows, show_meta: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    per_show: dict[int, dict] = defaultdict(lambda: {"plays": 0, "minutes": 0, "episodes": set()})
    per_movie: dict[int, dict] = {}
    for media_type, mid, m_title, m_poster, m_tmdb_id, show_id, runtime in top_rows:
        minutes = runtime or 0
        if media_type == "episode" and show_id:
            s = per_show[show_id]
            s["plays"] += 1
            s["minutes"] += minutes
            s["episodes"].add(mid)
        elif media_type == "movie":
            m = per_movie.setdefault(mid, {
                "title": m_title, "poster_path": m_poster, "tmdb_id": m_tmdb_id,
                "plays": 0, "minutes": 0,
            })
            m["plays"] += 1
            m["minutes"] += minutes

    top_shows = sorted(
        ({**show_meta.get(sid, {"title": None, "poster_path": None, "tmdb_id": None, "tvdb_id": None}),
          "plays": s["plays"], "minutes": s["minutes"], "episodes": len(s["episodes"])}
         for sid, s in per_show.items()),
        key=lambda x: (x["plays"], x["minutes"]), reverse=True,
    )
    top_movies = sorted(per_movie.values(), key=lambda x: (x["plays"], x["minutes"]), reverse=True)
    return top_shows, top_movies


async def _top_media(db: AsyncSession, user_id: int, date_filters: list) -> tuple[list[dict], list[dict]]:
    effective_runtime = effective_runtime_expression()
    top_rows = (await db.execute(
        select(
            Media.media_type, Media.id, Media.title, Media.poster_path,
            Media.tmdb_id, Media.show_id, effective_runtime.label("runtime"),
        )
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == user_id,
            Media.media_type.in_(["movie", "episode"]),
            *date_filters,
        )
    )).all()

    show_ids = {show_id for media_type, _, _, _, _, show_id, _ in top_rows if media_type == "episode" and show_id}
    show_meta: dict[int, dict] = {}
    if show_ids:
        top_show_rows = (await db.execute(
            select(ShowModel).where(ShowModel.id.in_(show_ids))
        )).scalars().all()
        for sh in top_show_rows:
            show_meta[sh.id] = {
                "title": sh.title, "poster_path": sh.poster_path,
                "tmdb_id": sh.tmdb_id, "tvdb_id": sh.tvdb_id,
            }
    return _rank_top_media(top_rows, show_meta)


@router.get("/{user_id}/most-played")
async def get_most_played(
    user_id: int,
    kind: Literal["shows", "movies"],
    since: Optional[DateType] = None,
    until: Optional[DateType] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_optional_user_or_api_key),
):
    await _check_profile_access(user_id, current_user, db)
    top_shows, top_movies = await _top_media(db, user_id, _watch_date_filters(since, until))
    return top_shows if kind == "shows" else top_movies
