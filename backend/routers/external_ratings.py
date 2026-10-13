"""Endpoints behind the selectable rating source (Settings -> Display)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import external_ratings
from db import get_db
from dependencies import get_current_user
from models.users import User, UserSettings

router = APIRouter()


async def _display_settings(db: AsyncSession, user_id: int) -> tuple[str, bool]:
    row = (await db.execute(
        select(UserSettings.rating_source, UserSettings.rating_show_tmdb).where(UserSettings.user_id == user_id)
    )).first()
    return (row[0] or "tmdb", bool(row[1])) if row else ("tmdb", False)


@router.get("/map")
async def get_rating_map(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The user's display settings plus every cached value of their chosen source.

    The frontend renders poster badges from this without a request per poster.
    """
    source, show_tmdb = await _display_settings(db, current_user.id)
    if source == "tmdb":
        return {"source": "tmdb", "show_tmdb": True, "values": {}, "stale": [], "missing": []}
    values, stale, missing = await external_ratings.cached_map(db, source)
    return {"source": source, "show_tmdb": show_tmdb, "values": values, "stale": stale, "missing": missing}


@router.get("/{media_type}/{tmdb_id}")
async def get_title_ratings(
    media_type: str,
    tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ratings for one title from every source MDBList has, refreshed when older than a day."""
    kind = external_ratings.MEDIA_TYPES.get(media_type)
    if not kind:
        raise HTTPException(status_code=400, detail="media_type must be movie or show")
    source, show_tmdb = await _display_settings(db, current_user.id)
    api_key = await external_ratings.resolve_api_key(db, current_user.id)
    ratings = await external_ratings.get_ratings(db, api_key, kind, tmdb_id)
    return {
        "source": source,
        "show_tmdb": show_tmdb,
        "ratings": ratings,
        "has_api_key": bool(api_key),
    }
