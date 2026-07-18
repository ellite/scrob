import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from db import get_db
from models.lists import List as ListModel, ListItem
from models.media import Media
from models.base import MediaType, PrivacyLevel
from models.show import Show as ShowModel
from models.users import UserSettings
from dependencies import get_current_user
from models.users import User
from routers.media import enrich_with_state

logger = logging.getLogger(__name__)

router = APIRouter()


class ListCreate(BaseModel):
    name: str
    description: Optional[str] = None
    privacy_level: PrivacyLevel = PrivacyLevel.private


class ListUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    privacy_level: Optional[PrivacyLevel] = None


class ListItemAdd(BaseModel):
    tmdb_id: int
    media_type: MediaType


def _format_list(lst: ListModel) -> dict:
    preview_posters: list[dict] = []
    for item in sorted(lst.items, key=lambda x: (x.sort_order, x.added_at)):
        if len(preview_posters) >= 3:
            break
        try:
            poster = item.media.poster_path
            if not poster and item.media.show:
                poster = item.media.show.poster_path
            if poster:
                preview_posters.append({"url": poster, "adult": item.media.adult})
        except Exception:
            pass
    return {
        "id": lst.id,
        "name": lst.name,
        "description": lst.description,
        "privacy_level": lst.privacy_level,
        "item_count": len(lst.items),
        "created_at": lst.created_at.isoformat(),
        "updated_at": lst.updated_at.isoformat(),
        "preview_posters": preview_posters,
    }


def _format_item(item: ListItem) -> dict:
    media = item.media
    data: dict = {
        "id": item.id,
        "list_id": item.list_id,
        "added_at": item.added_at.isoformat(),
        "sort_order": item.sort_order,
        "notes": item.notes,
        "media": {
            "id": media.id,
            "tmdb_id": media.tmdb_id,
            "type": media.media_type,
            "title": media.title,
            "poster_path": media.poster_path,
            "backdrop_path": media.backdrop_path,
            "release_date": media.release_date,
            "tmdb_rating": media.tmdb_rating,
            "season_number": media.season_number,
            "episode_number": media.episode_number,
            "adult": media.adult,
            "library": None,
            "in_library": False,
        },
    }
    if media.media_type == MediaType.episode and media.show:
        data["media"]["show_title"] = media.show.title
        data["media"]["show_poster_path"] = media.show.poster_path
        data["media"]["show_tmdb_id"] = media.show.tmdb_id
    return data


@router.get("/public")
async def get_public_lists(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ListModel, User.username)
        .join(User, User.id == ListModel.user_id)
        .options(selectinload(ListModel.items).selectinload(ListItem.media).selectinload(Media.show))
        .where(ListModel.privacy_level == PrivacyLevel.public, ListModel.user_id != current_user.id)
        .order_by(func.random())
        .limit(3)
    )
    rows = result.all()
    return {
        "lists": [
            {
                **_format_list(lst),
                "username": username,
            }
            for lst, username in rows
        ]
    }


@router.get("")
async def get_lists(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ListModel)
        .options(selectinload(ListModel.items).selectinload(ListItem.media).selectinload(Media.show))
        .where(ListModel.user_id == current_user.id)
        .order_by(ListModel.updated_at.desc())
    )
    lists = result.scalars().all()
    return {"lists": [_format_list(lst) for lst in lists]}


@router.post("", status_code=201)
async def create_list(
    body: ListCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lst = ListModel(
        user_id=current_user.id,
        name=body.name,
        description=body.description,
        privacy_level=body.privacy_level,
    )
    db.add(lst)
    await db.commit()
    await db.refresh(lst)
    return {
        "id": lst.id,
        "name": lst.name,
        "description": lst.description,
        "privacy_level": lst.privacy_level,
        "item_count": 0,
        "created_at": lst.created_at.isoformat(),
        "updated_at": lst.updated_at.isoformat(),
        "preview_posters": [],
    }


@router.get("/{list_id}")
async def get_list(
    list_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ListModel)
        .options(
            selectinload(ListModel.items)
            .selectinload(ListItem.media)
            .selectinload(Media.show)
        )
        .where(ListModel.id == list_id)
    )
    lst = result.scalar_one_or_none()
    if not lst:
        raise HTTPException(status_code=404, detail="List not found")
    if lst.user_id != current_user.id and lst.privacy_level == PrivacyLevel.private:
        raise HTTPException(status_code=403, detail="Access denied")

    items_sorted = sorted(lst.items, key=lambda x: (x.sort_order, x.added_at))
    formatted_items = [_format_item(i) for i in items_sorted]

    # Fill in missing poster/release_date for series items from the Show table
    series_tmdb_ids = [
        item["media"]["tmdb_id"]
        for item in formatted_items
        if item["media"].get("type") in (MediaType.series, "series")
        and (not item["media"].get("poster_path") or not item["media"].get("release_date"))
        and item["media"].get("tmdb_id")
    ]
    if series_tmdb_ids:
        shows_result = await db.execute(
            select(ShowModel).where(ShowModel.tmdb_id.in_(series_tmdb_ids))
        )
        show_map = {s.tmdb_id: s for s in shows_result.scalars().all()}
        for item in formatted_items:
            m = item["media"]
            if m.get("type") not in (MediaType.series, "series"):
                continue
            show = show_map.get(m.get("tmdb_id"))
            if show:
                if not m.get("poster_path") and show.poster_path:
                    m["poster_path"] = show.poster_path
                if not m.get("release_date") and show.first_air_date:
                    m["release_date"] = show.first_air_date
                if not m.get("title") and show.title:
                    m["title"] = show.title

    media_dicts = [item["media"] for item in formatted_items]
    await enrich_with_state(db, current_user.id, media_dicts)

    return {
        **_format_list(lst),
        "items": formatted_items,
        "is_owner": lst.user_id == current_user.id,
    }


@router.patch("/{list_id}")
async def update_list(
    list_id: int,
    body: ListUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ListModel).where(ListModel.id == list_id, ListModel.user_id == current_user.id)
    )
    lst = result.scalar_one_or_none()
    if not lst:
        raise HTTPException(status_code=404, detail="List not found")

    if body.name is not None:
        lst.name = body.name
    if body.description is not None:
        lst.description = body.description
    if body.privacy_level is not None:
        lst.privacy_level = body.privacy_level

    await db.commit()

    result = await db.execute(
        select(ListModel)
        .options(selectinload(ListModel.items))
        .where(ListModel.id == list_id)
    )
    lst = result.scalar_one()
    return _format_list(lst)


@router.delete("/{list_id}")
async def delete_list(
    list_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ListModel).where(ListModel.id == list_id, ListModel.user_id == current_user.id)
    )
    lst = result.scalar_one_or_none()
    if not lst:
        raise HTTPException(status_code=404, detail="List not found")
    await db.delete(lst)
    await db.commit()
    return {"message": "List deleted"}


def _trakt_media_type(media_type: MediaType) -> Optional[str]:
    if media_type == MediaType.movie:
        return "movies"
    if media_type == MediaType.series:
        return "shows"
    return None


async def _push_list_item_to_plex_watchlist(
    db: AsyncSession,
    user_id: int,
    media: Media,
    remove: bool = False,
) -> None:
    if not media.tmdb_id or media.media_type not in (MediaType.movie, MediaType.series):
        return
    from models.connections import MediaServerConnection
    from sqlalchemy import select as _select
    conns_result = await db.execute(
        _select(MediaServerConnection).where(
            MediaServerConnection.user_id == user_id,
            MediaServerConnection.type == "plex",
            MediaServerConnection.plex_push_watchlist == True,
        )
    )
    conns = conns_result.scalars().all()
    if not conns:
        return
    from core import plex as plex_client
    plex_type = "movie" if media.media_type == MediaType.movie else "show"
    for conn in conns:
        try:
            rating_key = await plex_client.resolve_tmdb_ratingkey(conn.token, media.tmdb_id, plex_type)
            if not rating_key:
                continue
            if remove:
                await plex_client.remove_from_watchlist(conn.token, rating_key)
            else:
                await plex_client.add_to_watchlist(conn.token, rating_key)
        except Exception as exc:
            logger.warning("Failed to push list item to Plex watchlist (conn=%s, remove=%s): %s", conn.id, remove, exc)


async def _push_list_item_to_trakt(
    db: AsyncSession,
    user_id: int,
    list_trakt_slug: str,
    media: Media,
    remove: bool = False,
) -> None:
    trakt_type = _trakt_media_type(media.media_type)
    if not trakt_type or not media.tmdb_id:
        return

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    settings = settings_result.scalar_one_or_none()
    if (
        not settings
        or not settings.trakt_push_lists
        or not settings.trakt_access_token
        or not settings.trakt_client_id
    ):
        return

    from core import trakt as trakt_client
    try:
        if list_trakt_slug in ("__watchlist__", "__watchlist_movies__", "__watchlist_shows__"):
            if remove:
                await trakt_client.remove_from_watchlist(
                    settings.trakt_client_id, settings.trakt_access_token,
                    trakt_type, media.tmdb_id,
                )
            else:
                await trakt_client.add_to_watchlist(
                    settings.trakt_client_id, settings.trakt_access_token,
                    trakt_type, media.tmdb_id,
                )
        else:
            if remove:
                await trakt_client.remove_from_list(
                    settings.trakt_client_id, settings.trakt_access_token,
                    list_trakt_slug, trakt_type, media.tmdb_id,
                )
            else:
                await trakt_client.add_to_list(
                    settings.trakt_client_id, settings.trakt_access_token,
                    list_trakt_slug, trakt_type, media.tmdb_id,
                )
    except Exception as exc:
        logger.warning("Failed to push list item to Trakt (slug=%s, remove=%s): %s", list_trakt_slug, remove, exc)



async def _push_list_item_to_mdblist(
    db: AsyncSession,
    user_id: int,
    list_mdblist_slug: str,
    media: Media,
    remove: bool = False,
) -> None:
    if (
        list_mdblist_slug != "__watchlist__"
        or not media.tmdb_id
        or media.media_type not in (MediaType.movie, MediaType.series)
    ):
        return

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    settings = settings_result.scalar_one_or_none()
    if not settings or not settings.mdblist_push_watchlist or not settings.mdblist_api_key:
        return

    from core import mdblist as mdblist_client

    kind = "movies" if media.media_type == MediaType.movie else "shows"
    payload = {"movies": [], "shows": [], "seasons": [], "episodes": []}
    payload[kind].append({"ids": {"tmdb": media.tmdb_id}})
    try:
        operation = mdblist_client.remove_watchlist if remove else mdblist_client.push_watchlist
        await operation(settings.mdblist_api_key, payload)
    except Exception as exc:
        logger.warning(
            "Failed to push list item to MDBList watchlist (remove=%s): %s",
            remove,
            exc,
        )


@router.post("/{list_id}/items", status_code=201)
async def add_list_item(
    list_id: int,
    body: ListItemAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    list_result = await db.execute(
        select(ListModel).where(ListModel.id == list_id, ListModel.user_id == current_user.id)
    )
    lst = list_result.scalar_one_or_none()
    if not lst:
        raise HTTPException(status_code=404, detail="List not found")

    media_result = await db.execute(
        select(Media)
        .options(selectinload(Media.show))
        .where(Media.tmdb_id == body.tmdb_id, Media.media_type == body.media_type)
    )
    media = media_result.scalar_one_or_none()

    from routers.media import get_user_tmdb_key
    from core import tmdb

    api_key = await get_user_tmdb_key(db, current_user.id)

    if not media:
        try:
            if body.media_type == MediaType.movie:
                data = await tmdb.get_movie(body.tmdb_id, api_key=api_key)
                media = Media(
                    tmdb_id=body.tmdb_id,
                    media_type=MediaType.movie,
                    title=data.get("title", "Unknown"),
                    poster_path=tmdb.poster_url(data.get("poster_path")),
                    backdrop_path=tmdb.poster_url(data.get("backdrop_path"), size="w1280"),
                    release_date=data.get("release_date"),
                    tmdb_rating=data.get("vote_average"),
                    overview=data.get("overview"),
                    adult=data.get("adult", False),
                )
            elif body.media_type == MediaType.person:
                data = await tmdb.get_person(body.tmdb_id, api_key=api_key)
                media = Media(
                    tmdb_id=body.tmdb_id,
                    media_type=MediaType.person,
                    title=data.get("name", "Unknown"),
                    poster_path=tmdb.poster_url(data.get("profile_path"), size="w185"),
                    overview=data.get("biography"),
                )
            else:
                data = await tmdb.get_show(body.tmdb_id, api_key=api_key)
                media = Media(
                    tmdb_id=body.tmdb_id,
                    media_type=MediaType.series,
                    title=data.get("name", "Unknown"),
                    poster_path=tmdb.poster_url(data.get("poster_path")),
                    backdrop_path=tmdb.poster_url(data.get("backdrop_path"), size="w1280"),
                    release_date=data.get("first_air_date"),
                    tmdb_rating=data.get("vote_average"),
                    overview=data.get("overview"),
                    adult=data.get("adult", False),
                )
            db.add(media)
            await db.flush()
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Media not found: {e}")
    elif not media.adult and body.media_type in (MediaType.movie, MediaType.series):
        # Existing record may pre-date the adult flag — refresh from TMDB
        try:
            if body.media_type == MediaType.movie:
                data = await tmdb.get_movie(body.tmdb_id, api_key=api_key)
            else:
                data = await tmdb.get_show(body.tmdb_id, api_key=api_key)
            if data.get("adult", False):
                media.adult = True
        except Exception:
            pass

    existing = await db.execute(
        select(ListItem).where(ListItem.list_id == list_id, ListItem.media_id == media.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Item already in list")

    item = ListItem(list_id=list_id, media_id=media.id)
    db.add(item)
    await db.commit()

    if lst.trakt_slug:
        await _push_list_item_to_trakt(db, current_user.id, lst.trakt_slug, media, remove=False)
        if lst.trakt_slug == "__plex_watchlist__":
            await _push_list_item_to_plex_watchlist(db, current_user.id, media, remove=False)

    if lst.mdblist_slug:
        await _push_list_item_to_mdblist(
            db, current_user.id, lst.mdblist_slug, media, remove=False
        )

    item_result = await db.execute(
        select(ListItem)
        .options(selectinload(ListItem.media).selectinload(Media.show))
        .where(ListItem.list_id == list_id, ListItem.media_id == media.id)
    )
    formatted = _format_item(item_result.scalar_one())
    await enrich_with_state(db, current_user.id, [formatted["media"]])
    return formatted


@router.delete("/{list_id}/items/{item_id}")
async def remove_list_item(
    list_id: int,
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ListItem)
        .options(selectinload(ListItem.media))
        .join(ListModel, ListModel.id == ListItem.list_id)
        .where(
            ListItem.id == item_id,
            ListItem.list_id == list_id,
            ListModel.user_id == current_user.id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    list_result = await db.execute(
        select(ListModel).where(ListModel.id == list_id)
    )
    lst = list_result.scalar_one_or_none()
    media = item.media

    await db.delete(item)
    await db.commit()

    if lst and lst.trakt_slug and media:
        await _push_list_item_to_trakt(db, current_user.id, lst.trakt_slug, media, remove=True)
        if lst.trakt_slug == "__plex_watchlist__":
            await _push_list_item_to_plex_watchlist(db, current_user.id, media, remove=True)

    if lst and lst.mdblist_slug and media:
        await _push_list_item_to_mdblist(
            db, current_user.id, lst.mdblist_slug, media, remove=True
        )

    return {"message": "Item removed"}
