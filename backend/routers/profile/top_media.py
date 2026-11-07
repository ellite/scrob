from datetime import date as DateType, timedelta as TimeDelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.media_runtime import effective_runtime_expression
from db import get_db
from dependencies import get_optional_user_or_api_key
from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.show import Show as ShowModel
from models.users import User
from .common import _check_profile_access

router = APIRouter()


def _watch_date_filters(since: Optional[DateType], until: Optional[DateType]) -> list:
    date_filters = [WatchEvent.completed == True]
    if since:
        date_filters.append(WatchEvent.watched_at >= since)
    if until:
        date_filters.append(WatchEvent.watched_at < until + TimeDelta(days=1))
    return date_filters


async def _top_media(
    db: AsyncSession, user_id: int, date_filters: list, limit: Optional[int] = None,
) -> tuple[list[dict], list[dict]]:
    """Most played shows and movies, ranked by plays then minutes. Each watch
    event is one play, so rewatches add up. The counting happens in the
    database (one row per title comes back, not one per play), and `limit`
    keeps only the top N of each."""
    plays = func.count(WatchEvent.id)
    minutes = func.coalesce(func.sum(effective_runtime_expression()), 0)

    movies_query = (
        select(Media.id, Media.title, Media.poster_path, Media.tmdb_id, plays.label("plays"), minutes.label("minutes"))
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type == MediaType.movie, *date_filters)
        .group_by(Media.id)
        .order_by(plays.desc(), minutes.desc(), Media.id)
    )
    shows_query = (
        select(
            Media.show_id, plays.label("plays"), minutes.label("minutes"),
            func.count(func.distinct(Media.id)).label("episodes"),
        )
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == user_id,
            Media.media_type == MediaType.episode,
            Media.show_id.isnot(None),
            *date_filters,
        )
        .group_by(Media.show_id)
        .order_by(plays.desc(), minutes.desc(), Media.show_id)
    )
    if limit:
        movies_query = movies_query.limit(limit)
        shows_query = shows_query.limit(limit)

    movie_rows = (await db.execute(movies_query)).all()
    show_rows = (await db.execute(shows_query)).all()

    show_meta: dict[int, dict] = {}
    if show_rows:
        shows = (await db.execute(
            select(ShowModel).where(ShowModel.id.in_([row.show_id for row in show_rows]))
        )).scalars().all()
        for sh in shows:
            show_meta[sh.id] = {
                "title": sh.title, "poster_path": sh.poster_path,
                "tmdb_id": sh.tmdb_id, "tvdb_id": sh.tvdb_id,
            }

    empty_show = {"title": None, "poster_path": None, "tmdb_id": None, "tvdb_id": None}
    top_shows = [
        {**show_meta.get(row.show_id, empty_show),
         "plays": row.plays, "minutes": int(row.minutes), "episodes": row.episodes}
        for row in show_rows
    ]
    top_movies = [
        {"title": row.title, "poster_path": row.poster_path, "tmdb_id": row.tmdb_id,
         "plays": row.plays, "minutes": int(row.minutes)}
        for row in movie_rows
    ]
    return top_shows, top_movies


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
