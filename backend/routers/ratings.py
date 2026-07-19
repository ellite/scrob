from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete

from db import get_db
from models.media import Media
from models.ratings import Rating
from models.base import MediaType
from models.users import UserSettings
from dependencies import get_current_user
from models.users import User
from core.enrichment import enrich_media

router = APIRouter()


class RatingIn(BaseModel):
    tmdb_id: int
    media_type: str
    rating: float = Field(..., ge=0.0, le=10.0)
    review: Optional[str] = None
    season_number: Optional[int] = None
    episode_order: Optional[str] = None


def format_rating(rating: Rating, media: Media) -> dict:
    return {
        "id": rating.id,
        "media": {
            "id": media.id,
            "tmdb_id": media.tmdb_id,
            "type": media.media_type,
            "title": media.title,
            "poster_path": media.poster_path,
            "release_date": media.release_date,
        },
        "season_number": rating.season_number,
        "episode_order": rating.episode_order,
        "user_id": rating.user_id,
        "rating": rating.rating,
        "review": rating.review,
        "rated_at": rating.rated_at.isoformat(),
    }


@router.delete("/all")
async def clear_all_ratings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await db.execute(delete(Rating).where(Rating.user_id == current_user.id))
    await db.commit()
    return {"status": "ok"}


@router.post("")
async def submit_rating(
    body: RatingIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        media_type = MediaType(body.media_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid media_type: {body.media_type}")

    # Look up existing Media row, create on-the-fly if missing
    result = await db.execute(
        select(Media).where(Media.tmdb_id == body.tmdb_id, Media.media_type == media_type)
    )
    media = result.scalar_one_or_none()

    if not media:
        from routers.media import get_user_tmdb_key
        from core import tmdb
        api_key = await get_user_tmdb_key(db, current_user.id)
        try:
            if media_type == MediaType.movie:
                data = await tmdb.get_movie(body.tmdb_id, api_key=api_key)
                title = data.get("title")
            elif media_type == MediaType.series:
                title = None  # enrich_media will populate all fields including title
            else:
                raise HTTPException(status_code=400, detail="Cannot create media row for episodes via rating")
            media = Media(tmdb_id=body.tmdb_id, media_type=media_type, title=title or "")
            db.add(media)
            await db.flush()
            await enrich_media(media, api_key=api_key)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"TMDB Media not found: {e}")

    effective_season = None if media_type == MediaType.episode else body.season_number
    effective_episode_order = (
        body.episode_order
        if media_type == MediaType.series and effective_season is not None
        else None
    )
    if effective_episode_order not in (None, "tvdb"):
        raise HTTPException(status_code=400, detail="Invalid episode order")

    result2 = await db.execute(
        select(Rating).where(
            Rating.media_id == media.id,
            Rating.user_id == current_user.id,
            Rating.season_number == effective_season,
            Rating.episode_order == effective_episode_order,
        )
    )
    rating = result2.scalar_one_or_none()

    if rating:
        rating.rating = body.rating
        rating.review = body.review
        rating.rated_at = datetime.utcnow()
    else:
        rating = Rating(
            media_id=media.id,
            user_id=current_user.id,
            rating=body.rating,
            review=body.review,
            season_number=effective_season,
            episode_order=effective_episode_order,
        )
        db.add(rating)

    await db.commit()
    await db.refresh(rating)
    if effective_episode_order == "tvdb":
        return format_rating(rating, media)

    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings = settings_result.scalar_one_or_none()
    from routers.sync import _fan_out_changes_to_other_connections

    await _fan_out_changes_to_other_connections(
        db,
        current_user.id,
        None,
        set(),
        {(media.id, effective_season): body.rating},
        settings=settings,
    )

    return format_rating(rating, media)


@router.get("")
async def get_ratings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Rating, Media)
        .join(Media, Media.id == Rating.media_id)
        .where(Rating.user_id == current_user.id)
        .order_by(desc(Rating.rated_at))
    )
    return {"results": [format_rating(r, m) for r, m in result.all()]}


@router.get("/{media_id}")
async def get_media_rating(
    media_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Rating, Media)
        .join(Media, Media.id == Rating.media_id)
        .where(Rating.media_id == media_id, Rating.user_id == current_user.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Rating not found")
    return format_rating(row[0], row[1])


@router.delete("")
async def delete_rating(
    tmdb_id: int,
    media_type: str,
    season_number: Optional[int] = Query(None),
    episode_order: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        mt = MediaType(media_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid media_type: {media_type}")

    media_result = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == mt)
    )
    media = media_result.scalar_one_or_none()
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    effective_season = None if mt == MediaType.episode else season_number
    effective_episode_order = (
        episode_order
        if mt == MediaType.series and effective_season is not None
        else None
    )

    result = await db.execute(
        select(Rating).where(
            Rating.media_id == media.id,
            Rating.user_id == current_user.id,
            Rating.season_number == effective_season,
            Rating.episode_order == effective_episode_order,
        )
    )
    rating = result.scalar_one_or_none()
    if not rating:
        raise HTTPException(status_code=404, detail="Rating not found")
    await db.delete(rating)
    await db.commit()
    if effective_episode_order == "tvdb":
        return {"status": "deleted"}

    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings = settings_result.scalar_one_or_none()
    from routers.sync import _fan_out_changes_to_other_connections

    await _fan_out_changes_to_other_connections(
        db,
        current_user.id,
        None,
        set(),
        {},
        settings=settings,
        removed_ratings={(media.id, effective_season)},
    )

    return {"status": "deleted"}
