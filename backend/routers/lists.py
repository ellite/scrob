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
from dependencies import get_current_user, get_current_user_or_api_key, get_optional_user_or_api_key
from models.users import User
from models.follows import Follow
from models.global_settings import GlobalSettings
from routers.media import enrich_with_state
from core.enrichment import is_unmapped_tvdb_episode, create_media_safely

logger = logging.getLogger(__name__)

router = APIRouter()


async def _check_list_access(lst: ListModel, current_user: Optional[User], db: AsyncSession) -> None:
    """Raises 403 if current_user (None for an anonymous viewer) can't view this list."""
    is_owner = bool(current_user and current_user.id == lst.user_id)
    is_admin = bool(current_user and current_user.role == "admin")

    is_mutual_follow = False
    if current_user and not is_owner and lst.privacy_level == PrivacyLevel.friends_only:
        mutual_q = await db.execute(
            select(func.count())
            .select_from(Follow)
            .where(Follow.follower_id == current_user.id, Follow.following_id == lst.user_id)
            .where(
                select(Follow.id)
                .where(Follow.follower_id == lst.user_id, Follow.following_id == current_user.id)
                .exists()
            )
        )
        is_mutual_follow = mutual_q.scalar_one() > 0

    if not (is_owner or is_admin or lst.privacy_level == PrivacyLevel.public or is_mutual_follow):
        raise HTTPException(status_code=403, detail="This list is private")

    # Same reasoning as _check_profile_access in routers/profile.py: a request
    # with no valid session only gets this far because the list is public.
    # Enforce the admin's global toggle here too, server-side, since the
    # frontend's page-level gate can be bypassed by attaching any api_key query
    # param (even an invalid one) to the proxied request.
    if not current_user and lst.privacy_level == PrivacyLevel.public:
        gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        gs = gs_result.scalar_one_or_none()
        if not (gs and gs.allow_public_profiles):
            raise HTTPException(status_code=403, detail="This list is private")


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
    season_number: Optional[int] = None


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
            "season_number": item.season_number if item.season_number is not None else media.season_number,
            "episode_number": media.episode_number,
            "adult": media.adult,
            "library": None,
            "in_library": False,
            "tvdb_sourced": is_unmapped_tvdb_episode(media),
        },
    }
    # Note: media.show is only populated for episode rows (via Media.show_id). A
    # season list item's media is the show's own Media row, so show_title/etc.
    # can't come from a relationship here - callers attach that separately by
    # looking up ShowModel from media.tmdb_id (see _attach_season_show_info).
    if media.media_type == MediaType.episode and media.show:
        data["media"]["show_title"] = media.show.title
        data["media"]["show_poster_path"] = media.show.poster_path
        data["media"]["show_tmdb_id"] = media.show.tmdb_id
        data["media"]["show_tvdb_id"] = media.show.tvdb_id
    return data


async def _attach_season_show_info(db: AsyncSession, media: dict) -> None:
    """Fill in show_title/show_poster_path/show_tvdb_id for a single season media
    dict - mirrors the batched version in get_list() for the single-item response
    add_list_item() returns."""
    if media.get("season_number") is None or not media.get("tmdb_id"):
        return
    show_result = await db.execute(select(ShowModel).where(ShowModel.tmdb_id == media["tmdb_id"]))
    show = show_result.scalar_one_or_none()
    if show:
        media["show_title"] = show.title
        media["show_poster_path"] = show.poster_path
        media["show_tmdb_id"] = show.tmdb_id
        media["show_tvdb_id"] = show.tvdb_id


@router.get("/public")
async def get_public_lists(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: Optional[User] = Depends(get_optional_user_or_api_key),
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
    await _check_list_access(lst, current_user, db)

    items_sorted = sorted(lst.items, key=lambda x: (x.sort_order, x.added_at))
    formatted_items = [_format_item(i) for i in items_sorted]

    # Fill in missing poster/release_date for series items from the Show table,
    # and attach show_title/show_poster_path/show_tvdb_id for season items -
    # their media.show relationship is always empty (see _format_item).
    series_tmdb_ids = {
        item["media"]["tmdb_id"]
        for item in formatted_items
        if item["media"].get("type") in (MediaType.series, "series")
        and item["media"].get("tmdb_id")
    }
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
            if not show:
                continue
            if not m.get("poster_path") and show.poster_path:
                m["poster_path"] = show.poster_path
            if not m.get("release_date") and show.first_air_date:
                m["release_date"] = show.first_air_date
            if not m.get("title") and show.title:
                m["title"] = show.title
            if m.get("season_number") is not None:
                m["show_title"] = show.title
                m["show_poster_path"] = show.poster_path
                m["show_tmdb_id"] = show.tmdb_id
                m["show_tvdb_id"] = show.tvdb_id

    media_dicts = [item["media"] for item in formatted_items]
    if current_user:
        await enrich_with_state(db, current_user.id, media_dicts)

    return {
        **_format_list(lst),
        "items": formatted_items,
        "is_owner": bool(current_user and lst.user_id == current_user.id),
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
    season_number: Optional[int] = None,
    remove: bool = False,
) -> None:
    # Plex watchlists have no season granularity - only whole movies/shows apply.
    if season_number is not None or not media.tmdb_id or media.media_type not in (MediaType.movie, MediaType.series):
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
            rating_key = await plex_client.resolve_tmdb_ratingkey(conn.token, media.tmdb_id, plex_type, media.title)
            if not rating_key:
                logger.warning(
                    "Could not resolve Plex ratingKey for tmdb_id=%s (%s), connection %s — skipping watchlist %s",
                    media.tmdb_id, plex_type, conn.id, "removal" if remove else "add",
                )
                continue
            if remove:
                await plex_client.remove_from_watchlist(conn.token, rating_key)
            else:
                await plex_client.add_to_watchlist(conn.token, rating_key)
        except Exception as exc:
            logger.warning("Failed to push list item to Plex watchlist (conn=%s, remove=%s): %s", conn.id, remove, exc)


async def _push_season_list_item_to_trakt(
    db: AsyncSession,
    user_id: int,
    list_trakt_slug: str,
    media: Media,
    season_number: int,
    remove: bool = False,
) -> None:
    # Trakt watchlists have no season granularity - only genuine lists support it.
    if list_trakt_slug.startswith("__watchlist") or not media.tmdb_id:
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

    from routers.media import get_user_tmdb_key
    from core import tmdb

    try:
        api_key = await get_user_tmdb_key(db, user_id)
        season_data = await tmdb.get_season(media.tmdb_id, season_number, api_key=api_key)
        season_tmdb_id = season_data.get("id")
    except Exception as exc:
        logger.warning(
            "Could not resolve TMDB season id for show=%s season=%s: %s",
            media.tmdb_id, season_number, exc,
        )
        return
    if not season_tmdb_id:
        return

    from core import trakt as trakt_client
    try:
        if remove:
            await trakt_client.remove_season_from_list(
                settings.trakt_client_id, settings.trakt_access_token,
                list_trakt_slug, season_tmdb_id,
            )
        else:
            await trakt_client.add_season_to_list(
                settings.trakt_client_id, settings.trakt_access_token,
                list_trakt_slug, season_tmdb_id,
            )
    except Exception as exc:
        logger.warning("Failed to push season list item to Trakt (slug=%s, remove=%s): %s", list_trakt_slug, remove, exc)


async def _push_list_item_to_trakt(
    db: AsyncSession,
    user_id: int,
    list_trakt_slug: str,
    media: Media,
    season_number: Optional[int] = None,
    remove: bool = False,
) -> None:
    if season_number is not None:
        await _push_season_list_item_to_trakt(db, user_id, list_trakt_slug, media, season_number, remove=remove)
        return

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
    season_number: Optional[int] = None,
    remove: bool = False,
) -> None:
    # MDBList watchlists have no season granularity - only whole movies/shows apply.
    if (
        season_number is not None
        or list_mdblist_slug != "__watchlist__"
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

    if body.season_number is not None and body.media_type != MediaType.series:
        raise HTTPException(status_code=400, detail="season_number is only valid for media_type=series")

    media_result = await db.execute(
        select(Media)
        .options(selectinload(Media.show))
        .where(Media.tmdb_id == body.tmdb_id, Media.media_type == body.media_type)
        .order_by(Media.id)
    )
    media = media_result.scalars().first()

    from routers.media import get_user_tmdb_key
    from core import tmdb

    api_key = await get_user_tmdb_key(db, current_user.id)

    if not media:
        try:
            if body.media_type == MediaType.movie:
                data = await tmdb.get_movie(body.tmdb_id, api_key=api_key)
                media, _created = await create_media_safely(
                    db, body.tmdb_id, MediaType.movie,
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
                media, _created = await create_media_safely(
                    db, body.tmdb_id, MediaType.person,
                    title=data.get("name", "Unknown"),
                    poster_path=tmdb.poster_url(data.get("profile_path"), size="w185"),
                    overview=data.get("biography"),
                )
            else:
                data = await tmdb.get_show(body.tmdb_id, api_key=api_key)
                media, _created = await create_media_safely(
                    db, body.tmdb_id, MediaType.series,
                    title=data.get("name", "Unknown"),
                    poster_path=tmdb.poster_url(data.get("poster_path")),
                    backdrop_path=tmdb.poster_url(data.get("backdrop_path"), size="w1280"),
                    release_date=data.get("first_air_date"),
                    tmdb_rating=data.get("vote_average"),
                    overview=data.get("overview"),
                    adult=data.get("adult", False),
                )
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

    if body.season_number is not None:
        try:
            await tmdb.get_season(body.tmdb_id, body.season_number, api_key=api_key)
        except Exception:
            raise HTTPException(status_code=404, detail="Season not found")

    season_key = func.coalesce(ListItem.season_number, -1)
    existing = await db.execute(
        select(ListItem).where(
            ListItem.list_id == list_id,
            ListItem.media_id == media.id,
            season_key == (body.season_number if body.season_number is not None else -1),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Item already in list")

    item = ListItem(list_id=list_id, media_id=media.id, season_number=body.season_number)
    db.add(item)
    await db.commit()

    if lst.trakt_slug:
        await _push_list_item_to_trakt(db, current_user.id, lst.trakt_slug, media, season_number=body.season_number, remove=False)
        if lst.trakt_slug == "__plex_watchlist__":
            await _push_list_item_to_plex_watchlist(db, current_user.id, media, season_number=body.season_number, remove=False)

    if lst.mdblist_slug:
        await _push_list_item_to_mdblist(
            db, current_user.id, lst.mdblist_slug, media, season_number=body.season_number, remove=False
        )

    item_result = await db.execute(
        select(ListItem)
        .options(selectinload(ListItem.media).selectinload(Media.show))
        .where(
            ListItem.list_id == list_id,
            ListItem.media_id == media.id,
            season_key == (body.season_number if body.season_number is not None else -1),
        )
    )
    formatted = _format_item(item_result.scalar_one())
    await _attach_season_show_info(db, formatted["media"])
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
    season_number = item.season_number

    await db.delete(item)
    await db.commit()

    if lst and lst.trakt_slug and media:
        await _push_list_item_to_trakt(db, current_user.id, lst.trakt_slug, media, season_number=season_number, remove=True)
        if lst.trakt_slug == "__plex_watchlist__":
            await _push_list_item_to_plex_watchlist(db, current_user.id, media, season_number=season_number, remove=True)

    if lst and lst.mdblist_slug and media:
        await _push_list_item_to_mdblist(
            db, current_user.id, lst.mdblist_slug, media, season_number=season_number, remove=True
        )

    return {"message": "Item removed"}
