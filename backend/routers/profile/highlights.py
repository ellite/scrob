from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from typing import Optional
from db import get_db
from dependencies import get_optional_user_or_api_key
from models.users import User
from models.events import WatchEvent
from models.media import Media
from models.ratings import Rating
from models.show import Show as ShowModel

from .common import _apply_viewer_language, _check_profile_access, _latest_episode_per_show_subquery


router = APIRouter()


_TOP_RATED_PAGE_SIZE = 24


@router.get("/{user_id}/top-rated-movies")
async def get_top_rated_movies(
    user_id: int,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user_or_api_key),
):
    await _check_profile_access(user_id, current_user, db)

    base_q = (
        select(Rating, Media)
        .join(Media, Rating.media_id == Media.id)
        .where(
            Rating.user_id == user_id,
            Rating.season_number.is_(None),
            Media.media_type == "movie",
            Rating.rating.isnot(None),
        )
    )
    total = (await db.execute(select(func.count()).select_from(base_q.subquery()))).scalar_one()

    rows = (await db.execute(
        base_q.order_by(Rating.rating.desc(), Rating.rated_at.desc(), Rating.id.desc())
        .offset((page - 1) * _TOP_RATED_PAGE_SIZE)
        .limit(_TOP_RATED_PAGE_SIZE)
    )).all()

    results = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "movie",
            "title": media.title,
            "poster_path": media.poster_path,
            "user_rating": rating.rating,
        }
        for rating, media in rows
    ]
    await _apply_viewer_language(db, current_user, [(i, m, None) for i, (_r, m) in zip(results, rows)])
    return {
        "page": page,
        "total_pages": max(1, (total + _TOP_RATED_PAGE_SIZE - 1) // _TOP_RATED_PAGE_SIZE),
        "total_results": total,
        "results": results,
    }


@router.get("/{user_id}/top-rated-shows")
async def get_top_rated_shows(
    user_id: int,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user_or_api_key),
):
    await _check_profile_access(user_id, current_user, db)

    base_q = (
        select(Rating, Media, ShowModel)
        .join(Media, Rating.media_id == Media.id)
        .outerjoin(ShowModel, (Media.media_type == "series") & (Media.tmdb_id == ShowModel.tmdb_id))
        .where(
            Rating.user_id == user_id,
            Rating.season_number.is_(None),
            Media.media_type == "series",
            Rating.rating.isnot(None),
        )
    )
    total = (await db.execute(select(func.count()).select_from(base_q.subquery()))).scalar_one()

    rows = (await db.execute(
        base_q.order_by(Rating.rating.desc(), Rating.rated_at.desc(), Rating.id.desc())
        .offset((page - 1) * _TOP_RATED_PAGE_SIZE)
        .limit(_TOP_RATED_PAGE_SIZE)
    )).all()

    results = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "series",
            "title": media.title,
            "poster_path": show.poster_path if show else media.poster_path,
            "user_rating": rating.rating,
        }
        for rating, media, show in rows
    ]
    await _apply_viewer_language(db, current_user, [(i, m, sh) for i, (_r, m, sh) in zip(results, rows)])
    return {
        "page": page,
        "total_pages": max(1, (total + _TOP_RATED_PAGE_SIZE - 1) // _TOP_RATED_PAGE_SIZE),
        "total_results": total,
        "results": results,
    }


_RECENTLY_WATCHED_PAGE_SIZE = 24


@router.get("/{user_id}/recently-watched-movies")
async def get_recently_watched_movies(
    user_id: int,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user_or_api_key),
):
    await _check_profile_access(user_id, current_user, db)

    base_q = (
        select(WatchEvent, Media)
        .join(Media, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == user_id,
            WatchEvent.completed == True,
            Media.media_type == "movie",
            WatchEvent.watched_at.isnot(None),
        )
    )
    total = (await db.execute(select(func.count()).select_from(base_q.subquery()))).scalar_one()

    rows = (await db.execute(
        base_q.order_by(WatchEvent.watched_at.desc(), WatchEvent.id.desc())
        .offset((page - 1) * _RECENTLY_WATCHED_PAGE_SIZE)
        .limit(_RECENTLY_WATCHED_PAGE_SIZE)
    )).all()

    results = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "movie",
            "title": media.title,
            "poster_path": media.poster_path,
            "watched_at": we.watched_at.isoformat(),
        }
        for we, media in rows
    ]
    await _apply_viewer_language(db, current_user, [(i, m, None) for i, (_w, m) in zip(results, rows)])
    return {
        "page": page,
        "total_pages": max(1, (total + _RECENTLY_WATCHED_PAGE_SIZE - 1) // _RECENTLY_WATCHED_PAGE_SIZE),
        "total_results": total,
        "results": results,
    }


@router.get("/{user_id}/recently-watched-shows")
async def get_recently_watched_shows(
    user_id: int,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user_or_api_key),
):
    await _check_profile_access(user_id, current_user, db)

    latest_episode_per_show = _latest_episode_per_show_subquery(user_id)
    base_q = (
        select(WatchEvent, Media, ShowModel)
        .join(latest_episode_per_show, WatchEvent.id == latest_episode_per_show.c.we_id)
        .join(Media, WatchEvent.media_id == Media.id)
        .outerjoin(ShowModel, Media.show_id == ShowModel.id)
    )
    total = (await db.execute(select(func.count()).select_from(base_q.subquery()))).scalar_one()

    rows = (await db.execute(
        base_q.order_by(WatchEvent.watched_at.desc(), WatchEvent.id.desc())
        .offset((page - 1) * _RECENTLY_WATCHED_PAGE_SIZE)
        .limit(_RECENTLY_WATCHED_PAGE_SIZE)
    )).all()

    results = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "episode",
            "title": media.title,
            "poster_path": show.poster_path if show else media.poster_path,
            "watched_at": we.watched_at.isoformat(),
            "show_title": show.title if show else None,
            "show_tmdb_id": show.tmdb_id if show else None,
            "show_tvdb_id": show.tvdb_id if show else None,
            "season_number": media.season_number,
            "episode_number": media.episode_number,
        }
        for we, media, show in rows
    ]
    await _apply_viewer_language(db, current_user, [(i, m, sh) for i, (_w, m, sh) in zip(results, rows)])
    return {
        "page": page,
        "total_pages": max(1, (total + _RECENTLY_WATCHED_PAGE_SIZE - 1) // _RECENTLY_WATCHED_PAGE_SIZE),
        "total_results": total,
        "results": results,
    }
