from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db import get_db
from models.users import User
from models.lists import List as UserList, ListItem
from models.media import Media, MediaType
from models.show import Show

router = APIRouter(tags=["compat"])


async def _user_by_api_key(
    x_api_key: str | None = Header(None, alias="X-Api-Key"),
    apikey: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    key = x_api_key or apikey
    if not key:
        raise HTTPException(401, "API key required")
    result = await db.execute(select(User).where(User.api_key == key))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(401, "Invalid API key")
    return user


async def _get_list(list_id: int, user: User, db: AsyncSession) -> UserList:
    lst = await db.get(UserList, list_id)
    if not lst or lst.user_id != user.id:
        raise HTTPException(404, "List not found")
    return lst


@router.get("/radarr-compat/{list_id}/api/v3/movie")
async def radarr_list(
    list_id: int,
    user: User = Depends(_user_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    await _get_list(list_id, user, db)

    rows = (await db.execute(
        select(Media)
        .join(ListItem, ListItem.media_id == Media.id)
        .where(ListItem.list_id == list_id, Media.media_type == MediaType.movie)
    )).scalars().all()

    result = []
    for media in rows:
        if not media.tmdb_id:
            continue
        year = int(media.release_date[:4]) if media.release_date else None
        result.append({
            "tmdbId": media.tmdb_id,
            "title": media.title,
            "originalTitle": media.original_title or media.title,
            "sortTitle": media.title.lower(),
            "overview": media.overview or "",
            "year": year,
            "runtime": media.runtime or 0,
            "status": media.status or "",
            "images": [],
            "genres": [],
            "tags": [],
            "ratings": {},
            "monitored": True,
            "hasFile": False,
            "isAvailable": True,
        })
    return result


@router.get("/sonarr-compat/{list_id}/api/v3/series")
async def sonarr_list(
    list_id: int,
    user: User = Depends(_user_by_api_key),
    db: AsyncSession = Depends(get_db),
):
    await _get_list(list_id, user, db)

    rows = (await db.execute(
        select(Media, Show.tvdb_id)
        .join(ListItem, ListItem.media_id == Media.id)
        .outerjoin(Show, Show.tmdb_id == Media.tmdb_id)
        .where(ListItem.list_id == list_id, Media.media_type == MediaType.series)
    )).all()

    result = []
    for media, tvdb_id in rows:
        if not media.tmdb_id:
            continue
        year = int(media.release_date[:4]) if media.release_date else None
        entry: dict = {
            "tmdbId": media.tmdb_id,
            "title": media.title,
            "sortTitle": media.title.lower(),
            "overview": media.overview or "",
            "year": year,
            "runtime": media.runtime or 0,
            "status": media.status or "",
            "images": [],
            "genres": [],
            "tags": [],
            "ratings": {"value": 0, "votes": 0},
            "seasons": [],
            "monitored": True,
            "seasonFolder": True,
        }
        if tvdb_id:
            entry["tvdbId"] = tvdb_id
        result.append(entry)
    return result
