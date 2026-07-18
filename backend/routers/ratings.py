from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete

from db import get_db
from models.media import Media
from models.ratings import Rating
from models.collection import Collection, CollectionFile
from models.base import CollectionSource, MediaType
from models.users import UserSettings
from models.connections import MediaServerConnection
from dependencies import get_current_user
from models.users import User
import core.plex as plex_client
import core.jellyfin as jellyfin_client
import core.emby as emby_client
import core.trakt as trakt_client
from core.enrichment import enrich_media

router = APIRouter()


class RatingIn(BaseModel):
    tmdb_id: int
    media_type: str
    rating: float = Field(..., ge=0.0, le=10.0)
    review: Optional[str] = None
    season_number: Optional[int] = None


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

    result2 = await db.execute(
        select(Rating).where(
            Rating.media_id == media.id,
            Rating.user_id == current_user.id,
            Rating.season_number == effective_season,
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
        )
        db.add(rating)

    await db.commit()
    await db.refresh(rating)

    # Fan-out rating push to all connections with push_ratings enabled
    import asyncio
    push_tasks = []
    conns_result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == current_user.id,
            MediaServerConnection.push_ratings == True,
        )
    )
    push_connections = conns_result.scalars().all()
    if push_connections:
        files_result = await db.execute(
            select(CollectionFile)
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .where(Collection.user_id == current_user.id, Collection.media_id == media.id)
        )
        coll_files = files_result.scalars().all()
        conn_by_type: dict[str, list] = {}
        for conn in push_connections:
            conn_by_type.setdefault(conn.type, []).append(conn)
        for coll_file in coll_files:
            if not coll_file.source_id:
                continue
            source_type = coll_file.source.value if hasattr(coll_file.source, "value") else str(coll_file.source)
            for conn in conn_by_type.get(source_type, []):
                if coll_file.source == CollectionSource.plex:
                    push_tasks.append(plex_client.set_rating(conn.url, conn.token, coll_file.source_id, body.rating))
                elif coll_file.source == CollectionSource.jellyfin:
                    push_tasks.append(jellyfin_client.set_rating(conn.url, conn.token, conn.server_user_id, coll_file.source_id, body.rating))
                elif coll_file.source == CollectionSource.emby:
                    push_tasks.append(emby_client.set_rating(conn.url, conn.token, conn.server_user_id, coll_file.source_id, body.rating))

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if settings and settings.trakt_push_ratings and settings.trakt_access_token and settings.trakt_client_id and media.tmdb_id:
        if media_type == MediaType.movie:
            push_tasks.append(trakt_client.set_movie_rating(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id, body.rating))
        elif media_type == MediaType.series:
            push_tasks.append(trakt_client.set_show_rating(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id, body.rating))
    if settings and settings.simkl_push_ratings and settings.simkl_access_token and settings.simkl_client_id and media.tmdb_id:
        from core import simkl as simkl_client
        if media_type == MediaType.movie:
            push_tasks.append(simkl_client.set_movie_rating(settings.simkl_client_id, settings.simkl_access_token, media.tmdb_id, body.rating))
        elif media_type == MediaType.series:
            push_tasks.append(simkl_client.set_show_rating(settings.simkl_client_id, settings.simkl_access_token, media.tmdb_id, body.rating))
    if settings and settings.mdblist_push_ratings and settings.mdblist_api_key:
        from core import mdblist as mdblist_client
        from routers.mdblist import _empty_payload, _payload_item

        payload = _empty_payload()
        item = _payload_item(media, rating=body.rating)
        if item:
            payload[item[0]].append(item[1])
            push_tasks.append(mdblist_client.push_ratings(settings.mdblist_api_key, payload))
    if push_tasks:
        await asyncio.gather(*push_tasks, return_exceptions=True)

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

    result = await db.execute(
        select(Rating).where(
            Rating.media_id == media.id,
            Rating.user_id == current_user.id,
            Rating.season_number == effective_season,
        )
    )
    rating = result.scalar_one_or_none()
    if not rating:
        raise HTTPException(status_code=404, detail="Rating not found")
    await db.delete(rating)
    await db.commit()

    import asyncio
    push_tasks = []
    conns_result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == current_user.id,
            MediaServerConnection.push_ratings == True,
        )
    )
    push_connections = conns_result.scalars().all()
    if push_connections:
        files_result = await db.execute(
            select(CollectionFile)
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .where(Collection.user_id == current_user.id, Collection.media_id == media.id)
        )
        coll_files = files_result.scalars().all()
        conn_by_type: dict[str, list] = {}
        for conn in push_connections:
            conn_by_type.setdefault(conn.type, []).append(conn)
        for coll_file in coll_files:
            if not coll_file.source_id:
                continue
            source_type = coll_file.source.value if hasattr(coll_file.source, "value") else str(coll_file.source)
            for conn in conn_by_type.get(source_type, []):
                if coll_file.source == CollectionSource.plex:
                    push_tasks.append(plex_client.set_rating(conn.url, conn.token, coll_file.source_id, 0))
                elif coll_file.source == CollectionSource.jellyfin:
                    push_tasks.append(jellyfin_client.set_rating(conn.url, conn.token, conn.server_user_id, coll_file.source_id, 0))
                elif coll_file.source == CollectionSource.emby:
                    push_tasks.append(emby_client.set_rating(conn.url, conn.token, conn.server_user_id, coll_file.source_id, 0))

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if settings and settings.trakt_push_ratings and settings.trakt_access_token and settings.trakt_client_id and media.tmdb_id:
        if mt == MediaType.movie:
            push_tasks.append(trakt_client.remove_movie_rating(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id))
        elif mt == MediaType.series:
            push_tasks.append(trakt_client.remove_show_rating(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id))
    if settings and settings.simkl_push_ratings and settings.simkl_access_token and settings.simkl_client_id and media.tmdb_id:
        from core import simkl as simkl_client
        if mt == MediaType.movie:
            push_tasks.append(simkl_client.remove_movie_rating(settings.simkl_client_id, settings.simkl_access_token, media.tmdb_id))
        elif mt == MediaType.series:
            push_tasks.append(simkl_client.remove_show_rating(settings.simkl_client_id, settings.simkl_access_token, media.tmdb_id))
    if settings and settings.mdblist_push_ratings and settings.mdblist_api_key:
        from core import mdblist as mdblist_client
        from routers.mdblist import _empty_payload, _payload_item

        payload = _empty_payload()
        item = _payload_item(media)
        if item:
            payload[item[0]].append(item[1])
            push_tasks.append(mdblist_client.remove_ratings(settings.mdblist_api_key, payload))
    if push_tasks:
        await asyncio.gather(*push_tasks, return_exceptions=True)

    return {"status": "deleted"}
