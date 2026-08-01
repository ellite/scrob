import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db import get_db
from models.users import User
from models.lists import List as UserList, ListItem
from models.media import Media, MediaType
from models.show import Show

log = logging.getLogger(__name__)

router = APIRouter(tags=["compat"])

TMDB_CONCURRENCY = 5  # Max concurrent TMDB requests


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
        # Radarr/Sonarr deserialize this into a non-nullable int — a null year
        # on any single item aborts the whole list import, so default to 0
        # (Radarr's own convention for unknown/unreleased year) instead.
        year = int(media.release_date[:4]) if media.release_date else 0
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
        select(Media, Show.tvdb_id, Show.tmdb_data)
        .join(ListItem, ListItem.media_id == Media.id)
        .outerjoin(Show, Show.tmdb_id == Media.tmdb_id)
        .where(ListItem.list_id == list_id, Media.media_type == MediaType.series)
    )).all()

    # Sonarr requires tvdbId to add a series from an import list. List items
    # added from TMDB search often lack a linked Show row with tvdb_id, so
    # fall back to the cached TMDB payload and finally to a live TMDB lookup —
    # mirroring the resolution order used by the manual "Request on Sonarr"
    # flow in routers/media.py.
    unresolved: list[tuple[int, int]] = []
    resolved: dict[int, int] = {}
    for idx, (media, tvdb_id, tmdb_data) in enumerate(rows):
        if not media.tmdb_id or tvdb_id:
            continue
        cached = (tmdb_data or {}).get("external_ids") or {}
        cached_tvdb = cached.get("tvdb_id") if isinstance(cached, dict) else None
        if cached_tvdb:
            resolved[idx] = cached_tvdb
        else:
            unresolved.append((idx, media.tmdb_id))

    if unresolved:
        from core import tmdb as tmdb_core
        from routers.media import get_user_tmdb_key

        tmdb_key = await get_user_tmdb_key(db, user.id)
        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

        async def _lookup(idx: int, tmdb_id: int) -> tuple[int, int | None]:
            try:
                async with semaphore:
                    ext = await tmdb_core.get_external_ids(tmdb_id, "tv", api_key=tmdb_key)
                return idx, ext.get("tvdb_id")
            except Exception as e:
                log.warning(f"sonarr-compat: TMDB external_ids lookup failed for tmdb:{tmdb_id}: {e}")
                return idx, None

        for idx, tvdb in await asyncio.gather(*[_lookup(i, t) for i, t in unresolved]):
            if tvdb:
                resolved[idx] = tvdb

    result = []
    for idx, (media, tvdb_id, _tmdb_data) in enumerate(rows):
        if not media.tmdb_id:
            continue
        # Radarr/Sonarr deserialize this into a non-nullable int — a null year
        # on any single item aborts the whole list import, so default to 0
        # (Radarr's own convention for unknown/unreleased year) instead.
        year = int(media.release_date[:4]) if media.release_date else 0
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
        effective_tvdb = tvdb_id or resolved.get(idx)
        if effective_tvdb:
            entry["tvdbId"] = effective_tvdb
        result.append(entry)
    return result
