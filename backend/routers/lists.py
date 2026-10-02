import asyncio
import logging
import re
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, and_, or_
from sqlalchemy.orm import selectinload, aliased

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
from routers.media import enrich_with_state, require_anon_nav_allowed
from core.enrichment import is_unmapped_tvdb_episode, create_media_safely, get_or_create_tvdb_series_media
from core.identity import find_media, find_show
from core.translations import (
    apply_media_translations,
    get_media_translations,
    get_show_translations,
    get_user_metadata_language,
)

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
        if not (gs and gs.enable_logged_out_navigation):
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
    # Any one of media_id / tmdb_id / tvdb_id identifies the item. Creating a
    # row that doesn't exist yet still needs a tmdb_id (it is fetched from
    # TMDB); a TVDB-only episode always exists locally already.
    tmdb_id: Optional[int] = None
    tvdb_id: Optional[int] = None
    media_id: Optional[int] = None
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
            "tmdb_id": media.tmdb_id, "tvdb_id": media.tvdb_id, "imdb_id": media.imdb_id,
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
    if media.get("season_number") is None or not (media.get("tmdb_id") or media.get("tvdb_id")):
        return
    show = await find_show(db, tmdb_id=media.get("tmdb_id"), tvdb_id=media.get("tvdb_id"))
    if show:
        media["show_title"] = show.title
        media["show_poster_path"] = show.poster_path
        media["show_tmdb_id"] = show.tmdb_id
        media["show_tvdb_id"] = show.tvdb_id


@router.get("/public")
async def get_public_lists(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_optional_user_or_api_key),
):
    if current_user is None:
        # Anonymous visitor: this becomes the whole /lists page rather than a
        # "From the Community" teaser alongside their own lists, so it's a
        # real (paginated-ish) browse of every public list, not a 3-item
        # random sample - there's no "self" to exclude and no follows to
        # union in either. Gated the same way as the other read-only pages.
        await require_anon_nav_allowed(db)
        result = await db.execute(
            select(ListModel, User.username)
            .join(User, User.id == ListModel.user_id)
            .options(selectinload(ListModel.items).selectinload(ListItem.media).selectinload(Media.show))
            .where(ListModel.privacy_level == PrivacyLevel.public)
            .order_by(ListModel.updated_at.desc())
            .limit(60)
        )
        rows = result.all()
        return {"lists": [{**_format_list(lst), "username": username} for lst, username in rows]}

    # Friends-only lists of mutual follows belong in discovery too — they were
    # invisible to the very friends they're shared with (#210).
    FollowBack = aliased(Follow)
    mutual_rows = await db.execute(
        select(Follow.following_id)
        .join(FollowBack, and_(
            FollowBack.follower_id == Follow.following_id,
            FollowBack.following_id == Follow.follower_id,
        ))
        .where(Follow.follower_id == current_user.id)
    )
    mutual_ids = [r[0] for r in mutual_rows.all()]
    visibility = ListModel.privacy_level == PrivacyLevel.public
    if mutual_ids:
        visibility = or_(visibility, and_(
            ListModel.privacy_level == PrivacyLevel.friends_only,
            ListModel.user_id.in_(mutual_ids),
        ))
    result = await db.execute(
        select(ListModel, User.username)
        .join(User, User.id == ListModel.user_id)
        .options(selectinload(ListModel.items).selectinload(ListItem.media).selectinload(Media.show))
        .where(visibility, ListModel.user_id != current_user.id)
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
    current_user: User = Depends(get_current_user_or_api_key),
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


class TmdbListImport(BaseModel):
    list_id_or_url: str


def _parse_tmdb_list_id(raw: str) -> Optional[int]:
    """Accepts a bare numeric TMDB list id, a full themoviedb.org/list/{id}-
    slug URL, or just the "{id}-slug" part someone copied without the domain
    (all three forms use the same leading-digits-then-dash convention)."""
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    m = re.search(r"/list/(\d+)", raw) or re.match(r"(\d+)-", raw)
    return int(m.group(1)) if m else None


# A single POST request resolves every item synchronously (no background
# job/polling UI exists for this yet, unlike Trakt/MDBList/etc.) - capped so
# a huge public list can't make that request run for minutes.
_TMDB_LIST_IMPORT_MAX_PAGES = 25  # 20 items/page = 500 items


@router.post("/import/tmdb", status_code=201)
async def import_tmdb_list(
    body: TmdbListImport,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    tmdb_list_id = _parse_tmdb_list_id(body.list_id_or_url)
    if tmdb_list_id is None:
        raise HTTPException(status_code=400, detail="Could not find a TMDB list ID in that input")

    from routers.media import get_user_tmdb_key
    from routers.trakt import _get_or_create_movie_media, _get_or_create_series_media
    from core import tmdb

    api_key = await get_user_tmdb_key(db, current_user.id)
    if not api_key:
        raise HTTPException(status_code=400, detail="TMDB API key required")

    try:
        first_page = await tmdb.get_list(tmdb_list_id, api_key=api_key, page=1)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"TMDB list not found (it may be private): {e}")

    total_pages = min(first_page.get("total_pages", 1) or 1, _TMDB_LIST_IMPORT_MAX_PAGES)
    pages = [first_page]
    for p in range(2, total_pages + 1):
        try:
            pages.append(await tmdb.get_list(tmdb_list_id, api_key=api_key, page=p))
        except Exception as exc:
            logger.warning("TMDB list %s: stopped early at page %s: %s", tmdb_list_id, p, exc)
            break

    existing_list_result = await db.execute(
        select(ListModel).where(
            ListModel.user_id == current_user.id,
            ListModel.tmdb_list_id == tmdb_list_id,
        )
    )
    lst = existing_list_result.scalar_one_or_none()
    if not lst:
        lst = ListModel(
            user_id=current_user.id,
            name=first_page.get("name") or f"TMDB List {tmdb_list_id}",
            description=first_page.get("description") or None,
            tmdb_list_id=tmdb_list_id,
        )
        db.add(lst)
        await db.flush()

    existing_media_ids_result = await db.execute(
        select(ListItem.media_id).where(ListItem.list_id == lst.id)
    )
    existing_media_ids = {row[0] for row in existing_media_ids_result}

    for page_data in pages:
        for entry in page_data.get("results", []):
            tmdb_id = entry.get("id")
            media_type = entry.get("media_type")
            if not tmdb_id or media_type not in ("movie", "tv"):
                continue
            try:
                async with db.begin_nested():
                    if media_type == "movie":
                        media = await _get_or_create_movie_media(
                            db, tmdb_id, entry.get("title") or entry.get("original_title") or "", api_key
                        )
                    else:
                        media = await _get_or_create_series_media(
                            db, tmdb_id, entry.get("name") or entry.get("original_name") or "", api_key
                        )
            except Exception as exc:
                logger.warning("Could not import TMDB list %s item tmdb_id=%s (%s): %s", tmdb_list_id, tmdb_id, media_type, exc)
                continue
            if not media or media.id in existing_media_ids:
                continue
            db.add(ListItem(list_id=lst.id, media_id=media.id))
            existing_media_ids.add(media.id)

    await db.commit()

    result = await db.execute(
        select(ListModel)
        .options(selectinload(ListModel.items).selectinload(ListItem.media).selectinload(Media.show))
        .where(ListModel.id == lst.id)
    )
    return _format_list(result.scalar_one())


class TvdbListImport(BaseModel):
    list_id_or_url: str


def _parse_tvdb_list_ref(raw: str) -> Optional[str]:
    """A bare numeric TVDB list id, OR the slug TVDB uses instead for
    "official" lists (thetvdb.com/lists/marvel-cinematic-universe has no
    numeric id anywhere in its own URL - user-created lists get a plain
    numeric id instead, but official ones don't). Accepts either form bare,
    or a full thetvdb.com/lists/{ref} URL. Always returned as a string; the
    caller checks .isdigit() to decide whether it still needs to resolve a
    slug to an id via TVDB's own lookup."""
    raw = raw.strip()
    m = re.search(r"/lists/([^/?#\s]+)", raw)
    ref = m.group(1) if m else raw
    if not ref or " " in ref:
        return None
    return ref


# Same reasoning as _TMDB_LIST_IMPORT_MAX_PAGES, but counting entities
# directly (TVDB's list-extended call isn't paginated - one request returns
# every entity). Kept lower than TMDB's cap since each entity here costs an
# *additional* TVDB call to resolve its TMDB cross-id.
_TVDB_LIST_IMPORT_MAX_ENTITIES = 300
_TVDB_LIST_IMPORT_CONCURRENCY = 8


@router.post("/import/tvdb", status_code=201)
async def import_tvdb_list(
    body: TvdbListImport,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    list_ref = _parse_tvdb_list_ref(body.list_id_or_url)
    if list_ref is None:
        raise HTTPException(status_code=400, detail="Could not find a TVDB list ID in that input")

    from routers.shows import get_user_tvdb_key
    from routers.media import get_user_tmdb_key
    from routers.trakt import _get_or_create_movie_media, _get_or_create_series_media
    from core import tvdb as tvdb_client

    tvdb_key = await get_user_tvdb_key(db, current_user.id)
    if not tvdb_key:
        raise HTTPException(status_code=400, detail="TVDB API key required")
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not tmdb_key:
        # Every entity is resolved through TMDB (see the module-level note on
        # _get_or_create_movie_media/_get_or_create_series_media) - a TVDB
        # key alone can find the list but not import anything from it.
        raise HTTPException(status_code=400, detail="TMDB API key also required to resolve TVDB list items")

    if list_ref.isdigit():
        tvdb_list_id = int(list_ref)
    else:
        try:
            tvdb_list_id = await tvdb_client.get_list_id_by_slug(list_ref, tvdb_key)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"TVDB list not found: {e}")
        if tvdb_list_id is None:
            raise HTTPException(status_code=404, detail="TVDB list not found")

    try:
        list_data = await tvdb_client.get_list(tvdb_list_id, tvdb_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"TVDB list not found: {e}")

    entities = (list_data.get("entities") or [])[:_TVDB_LIST_IMPORT_MAX_ENTITIES]

    # Resolving each entity's TMDB cross-id is its own TVDB call - the part
    # of this import that's genuinely "so many calls" (#442 follow-up) - so
    # these run bounded-concurrently instead of one at a time, the same
    # pattern backend/routers/calendar.py already uses for its own per-show
    # TMDB fan-out.
    sem = asyncio.Semaphore(_TVDB_LIST_IMPORT_CONCURRENCY)

    async def _resolve(entity: dict) -> tuple[str, int, int | None] | None:
        series_id = entity.get("seriesId")
        movie_id = entity.get("movieId")
        async with sem:
            try:
                if series_id:
                    return ("series", series_id, await tvdb_client.get_series_tmdb_cross_id(series_id, tvdb_key))
                if movie_id:
                    return ("movie", movie_id, await tvdb_client.get_movie_tmdb_cross_id(movie_id, tvdb_key))
            except Exception as exc:
                logger.warning("TVDB list %s: could not resolve entity %s: %s", tvdb_list_id, entity, exc)
        return None

    resolved = await asyncio.gather(*(_resolve(e) for e in entities))

    existing_list_result = await db.execute(
        select(ListModel).where(
            ListModel.user_id == current_user.id,
            ListModel.tvdb_list_id == tvdb_list_id,
        )
    )
    lst = existing_list_result.scalar_one_or_none()
    if not lst:
        lst = ListModel(
            user_id=current_user.id,
            name=list_data.get("name") or f"TVDB List {tvdb_list_id}",
            description=list_data.get("overview") or None,
            tvdb_list_id=tvdb_list_id,
        )
        db.add(lst)
        await db.flush()

    existing_media_ids_result = await db.execute(
        select(ListItem.media_id).where(ListItem.list_id == lst.id)
    )
    existing_media_ids = {row[0] for row in existing_media_ids_result}

    for entry in resolved:
        if not entry:
            continue
        kind, _tvdb_entity_id, tmdb_id = entry
        if not tmdb_id:
            continue  # no TMDB counterpart on file for this show/movie - skip it
        try:
            async with db.begin_nested():
                if kind == "movie":
                    media = await _get_or_create_movie_media(db, tmdb_id, f"TMDB {tmdb_id}", tmdb_key)
                else:
                    media = await _get_or_create_series_media(db, tmdb_id, f"TMDB {tmdb_id}", tmdb_key)
        except Exception as exc:
            logger.warning("Could not import TVDB list %s item tmdb_id=%s (%s): %s", tvdb_list_id, tmdb_id, kind, exc)
            continue
        if not media or media.id in existing_media_ids:
            continue
        db.add(ListItem(list_id=lst.id, media_id=media.id))
        existing_media_ids.add(media.id)

    await db.commit()

    result = await db.execute(
        select(ListModel)
        .options(selectinload(ListModel.items).selectinload(ListItem.media).selectinload(Media.show))
        .where(ListModel.id == lst.id)
    )
    return _format_list(result.scalar_one())


class ImdbListImport(BaseModel):
    list_id_or_url: str


def _parse_imdb_list_id(raw: str) -> Optional[str]:
    """Accept a bare ``ls...`` id or an imdb.com/list/ls... URL."""
    raw = raw.strip()
    if re.fullmatch(r"ls\d+", raw, flags=re.IGNORECASE):
        return raw.lower()
    match = re.search(r"/list/(ls\d+)(?:[/?#]|$)", raw, flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


# IMDb returns cursor-paginated pages. Keep this synchronous endpoint bounded
# to the same 500-item ceiling as TMDB, then resolve external ids concurrently.
_IMDB_LIST_IMPORT_PAGE_SIZE = 100
_IMDB_LIST_IMPORT_MAX_PAGES = 5
_IMDB_LIST_IMPORT_CONCURRENCY = 8


@router.post("/import/imdb", status_code=201)
async def import_imdb_list(
    body: ImdbListImport,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    imdb_list_id = _parse_imdb_list_id(body.list_id_or_url)
    if imdb_list_id is None:
        raise HTTPException(status_code=400, detail="Could not find an IMDb list ID in that input")

    from core import imdb as imdb_client, tmdb
    from routers.media import get_user_tmdb_key
    from routers.trakt import _get_or_create_movie_media, _get_or_create_series_media

    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not tmdb_key:
        raise HTTPException(status_code=400, detail="TMDB API key required to resolve IMDb list items")

    pages: list[dict] = []
    after: str | None = None
    try:
        for _page_number in range(_IMDB_LIST_IMPORT_MAX_PAGES):
            page = await imdb_client.get_public_list_page(
                imdb_list_id,
                after=after,
                first=_IMDB_LIST_IMPORT_PAGE_SIZE,
            )
            pages.append(page)
            page_info = ((page.get("titleListItemSearch") or {}).get("pageInfo") or {})
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
    except Exception as exc:
        if not pages:
            raise HTTPException(
                status_code=404,
                detail=f"IMDb list not found (it may be private): {exc}",
            )
        logger.warning("IMDb list %s: stopped early after %s page(s): %s", imdb_list_id, len(pages), exc)

    first_page = pages[0]
    entries = [
        edge.get("title") or {}
        for page in pages
        for edge in ((page.get("titleListItemSearch") or {}).get("edges") or [])
    ]

    sem = asyncio.Semaphore(_IMDB_LIST_IMPORT_CONCURRENCY)

    async def _resolve(entry: dict) -> tuple[str, int, str] | None:
        imdb_id = entry.get("id")
        if not isinstance(imdb_id, str) or not re.fullmatch(r"tt\d+", imdb_id):
            return None
        async with sem:
            try:
                found = await tmdb.find_by_external_id(imdb_id, "imdb_id", api_key=tmdb_key)
            except Exception as exc:
                logger.warning("IMDb list %s: could not resolve title %s: %s", imdb_list_id, imdb_id, exc)
                return None

        title = ((entry.get("titleText") or {}).get("text") or imdb_id)
        title_type = (entry.get("titleType") or {}).get("id")
        movie_results = found.get("movie_results") or []
        tv_results = found.get("tv_results") or []
        if title_type in {"tvSeries", "tvMiniSeries"} and tv_results:
            return ("series", tv_results[0]["id"], title)
        if movie_results:
            return ("movie", movie_results[0]["id"], title)
        if tv_results:
            return ("series", tv_results[0]["id"], title)
        # Individual episodes and unsupported IMDb title types have neither a
        # movie nor whole-series match in TMDB and are intentionally skipped.
        return None

    resolved = await asyncio.gather(*(_resolve(entry) for entry in entries))

    existing_list_result = await db.execute(
        select(ListModel).where(
            ListModel.user_id == current_user.id,
            ListModel.imdb_list_id == imdb_list_id,
        )
    )
    lst = existing_list_result.scalar_one_or_none()
    if not lst:
        name = ((first_page.get("name") or {}).get("originalText") or f"IMDb List {imdb_list_id}")
        description = (((first_page.get("description") or {}).get("originalText") or {}).get("plainText"))
        lst = ListModel(
            user_id=current_user.id,
            name=name[:255],
            description=description or None,
            imdb_list_id=imdb_list_id,
        )
        db.add(lst)
        await db.flush()

    existing_media_ids_result = await db.execute(
        select(ListItem.media_id).where(ListItem.list_id == lst.id)
    )
    existing_media_ids = {row[0] for row in existing_media_ids_result}

    for entry in resolved:
        if not entry:
            continue
        kind, tmdb_id, title = entry
        try:
            async with db.begin_nested():
                if kind == "movie":
                    media = await _get_or_create_movie_media(db, tmdb_id, title, tmdb_key)
                else:
                    media = await _get_or_create_series_media(db, tmdb_id, title, tmdb_key)
        except Exception as exc:
            logger.warning(
                "Could not import IMDb list %s item tmdb_id=%s (%s): %s",
                imdb_list_id, tmdb_id, kind, exc,
            )
            continue
        if not media or media.id in existing_media_ids:
            continue
        db.add(ListItem(list_id=lst.id, media_id=media.id))
        existing_media_ids.add(media.id)

    await db.commit()

    result = await db.execute(
        select(ListModel)
        .options(selectinload(ListModel.items).selectinload(ListItem.media).selectinload(Media.show))
        .where(ListModel.id == lst.id)
    )
    return _format_list(result.scalar_one())


class MdblistListImport(BaseModel):
    list_id_or_url: str


def _parse_mdblist_list_ref(raw: str) -> Optional[str]:
    """Accept a numeric list id, username/slug, or mdblist.com list URL."""
    raw = raw.strip()
    if raw.isdigit():
        return raw
    match = re.search(
        r"(?:https?://)?(?:www\.)?mdblist\.com/lists/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)(?:[/?#]|$)",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{match.group(1).lower()}/{match.group(2).lower()}"
    if re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", raw):
        return raw.lower()
    return None


@router.post("/import/mdblist", status_code=201)
async def import_mdblist_list(
    body: MdblistListImport,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    list_ref = _parse_mdblist_list_ref(body.list_id_or_url)
    if list_ref is None:
        raise HTTPException(status_code=400, detail="Could not find an MDBList list ID or URL in that input")

    from core import mdblist as mdblist_client
    from routers.media import get_user_tmdb_key
    from routers.mdblist import _resolve_media

    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings = settings_result.scalar_one_or_none()
    global_settings_result = await db.execute(
        select(GlobalSettings).where(GlobalSettings.id == 1)
    )
    global_settings = global_settings_result.scalar_one_or_none()
    mdblist_api_key = (
        settings.mdblist_api_key if settings and settings.mdblist_api_key
        else global_settings.mdblist_api_key if global_settings else None
    )
    if not mdblist_api_key:
        raise HTTPException(status_code=400, detail="MDBList API key required")

    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not tmdb_key:
        raise HTTPException(status_code=400, detail="TMDB API key required to resolve MDBList items")

    try:
        list_data = await mdblist_client.get_list(mdblist_api_key, list_ref)
        items = await mdblist_client.get_list_items(
            mdblist_api_key,
            list_ref,
            max_items=500,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"MDBList list not found (it may be private): {exc}",
        )

    remote_id = list_data.get("id")
    remote_key = f"list:{remote_id}" if remote_id is not None else f"list:{list_ref}"
    existing_list_result = await db.execute(
        select(ListModel).where(
            ListModel.user_id == current_user.id,
            ListModel.mdblist_slug == remote_key,
        )
    )
    lst = existing_list_result.scalar_one_or_none()
    if not lst:
        name = str(list_data.get("name") or f"MDBList {list_ref}")
        lst = ListModel(
            user_id=current_user.id,
            name=name[:255],
            mdblist_slug=remote_key,
        )
        db.add(lst)
        await db.flush()

    existing_media_ids_result = await db.execute(
        select(ListItem.media_id).where(ListItem.list_id == lst.id)
    )
    existing_media_ids = {row[0] for row in existing_media_ids_result}
    external_cache: dict[tuple[str, str], int | None] = {}

    for kind in ("movies", "shows"):
        for entry in items.get(kind, []):
            if not isinstance(entry, dict):
                continue
            try:
                async with db.begin_nested():
                    media = await _resolve_media(
                        db,
                        kind,
                        entry,
                        tmdb_key,
                        external_cache,
                    )
            except Exception as exc:
                logger.warning(
                    "Could not import MDBList %s item %s: %s",
                    list_ref,
                    entry.get("id") or entry.get("ids"),
                    exc,
                )
                continue
            if not media or media.id in existing_media_ids:
                continue
            db.add(ListItem(list_id=lst.id, media_id=media.id))
            existing_media_ids.add(media.id)

    await db.commit()

    result = await db.execute(
        select(ListModel)
        .options(selectinload(ListModel.items).selectinload(ListItem.media).selectinload(Media.show))
        .where(ListModel.id == lst.id)
    )
    return _format_list(result.scalar_one())


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
    # TheTVDB-only series rows have no tmdb_id - matched by tvdb_id instead.
    series_tvdb_only_ids = {
        item["media"]["tvdb_id"]
        for item in formatted_items
        if item["media"].get("type") in (MediaType.series, "series")
        and not item["media"].get("tmdb_id")
        and item["media"].get("tvdb_id")
    }
    show_map: dict = {}
    show_by_tvdb: dict = {}
    if series_tmdb_ids:
        shows_result = await db.execute(
            select(ShowModel).where(ShowModel.tmdb_id.in_(series_tmdb_ids))
        )
        show_map = {s.tmdb_id: s for s in shows_result.scalars().all()}
    if series_tvdb_only_ids:
        tvdb_shows_result = await db.execute(
            select(ShowModel).where(ShowModel.tvdb_id.in_(series_tvdb_only_ids))
        )
        show_by_tvdb = {s.tvdb_id: s for s in tvdb_shows_result.scalars().all()}
    if show_map or show_by_tvdb:
        for item in formatted_items:
            m = item["media"]
            if m.get("type") not in (MediaType.series, "series"):
                continue
            show = show_map.get(m.get("tmdb_id")) if m.get("tmdb_id") else show_by_tvdb.get(m.get("tvdb_id"))
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

    # Apply the viewer's metadata language, same as detail pages and history
    # do - list items were the one place translations never reached (#221).
    # Movies/episodes translate via MediaTranslation on their own media id;
    # series (and season) items translate via ShowTranslation on the Show row.
    lang = await get_user_metadata_language(db, current_user.id) if current_user else None
    if lang:
        translations = await get_media_translations(
            db, [m["id"] for m in media_dicts if m.get("id")], lang
        )
        apply_media_translations(media_dicts, translations)

        show_ids = {i.media.show_id for i in items_sorted if i.media.show_id}
        show_by_tmdb = {tmdb_id: s.id for tmdb_id, s in show_map.items()}
        show_ids.update(show_by_tmdb.values())
        if show_ids:
            show_translations = await get_show_translations(db, list(show_ids), lang)
            for item, li in zip(formatted_items, items_sorted):
                m = item["media"]
                t = None
                if m.get("type") in (MediaType.series, "series"):
                    t = show_translations.get(show_by_tmdb.get(m.get("tmdb_id")))
                    if t and t.get("title") and m.get("season_number") is None:
                        m["title"] = t["title"]
                elif li.media.show_id:
                    t = show_translations.get(li.media.show_id)
                if t and t.get("title") and m.get("show_title"):
                    m["show_title"] = t["title"]

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
    current_user: User = Depends(get_current_user_or_api_key),
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


@router.delete("/all")
async def clear_all_lists(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    # ListItem rows cascade via the FK's ondelete=CASCADE - no separate delete needed.
    await db.execute(delete(ListModel).where(ListModel.user_id == current_user.id))
    await db.commit()
    return {"status": "ok"}


@router.delete("/{list_id}")
async def delete_list(
    list_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
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
            rating_key = await plex_client.resolve_tmdb_ratingkey(conn.plex_account_token, media.tmdb_id, plex_type, media.title)
            if not rating_key:
                logger.warning(
                    "Could not resolve Plex ratingKey for tmdb_id=%s (%s), connection %s — skipping watchlist %s",
                    media.tmdb_id, plex_type, conn.id, "removal" if remove else "add",
                )
                continue
            if remove:
                await plex_client.remove_from_watchlist(conn.plex_account_token, rating_key)
            else:
                await plex_client.add_to_watchlist(conn.plex_account_token, rating_key)
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
    from routers.trakt import ensure_valid_trakt_token
    try:
        token = await ensure_valid_trakt_token(db, settings)
        if remove:
            await trakt_client.remove_season_from_list(
                settings.trakt_client_id, token,
                list_trakt_slug, season_tmdb_id,
            )
        else:
            await trakt_client.add_season_to_list(
                settings.trakt_client_id, token,
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
    from routers.trakt import ensure_valid_trakt_token
    try:
        token = await ensure_valid_trakt_token(db, settings)
        if list_trakt_slug in ("__watchlist__", "__watchlist_movies__", "__watchlist_shows__"):
            if remove:
                await trakt_client.remove_from_watchlist(
                    settings.trakt_client_id, token,
                    trakt_type, media.tmdb_id,
                )
            else:
                await trakt_client.add_to_watchlist(
                    settings.trakt_client_id, token,
                    trakt_type, media.tmdb_id,
                )
        else:
            if remove:
                await trakt_client.remove_from_list(
                    settings.trakt_client_id, token,
                    list_trakt_slug, trakt_type, media.tmdb_id,
                )
            else:
                await trakt_client.add_to_list(
                    settings.trakt_client_id, token,
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
    current_user: User = Depends(get_current_user_or_api_key),
):
    list_result = await db.execute(
        select(ListModel).where(ListModel.id == list_id, ListModel.user_id == current_user.id)
    )
    lst = list_result.scalar_one_or_none()
    if not lst:
        raise HTTPException(status_code=404, detail="List not found")

    if body.season_number is not None and body.media_type != MediaType.series:
        raise HTTPException(status_code=400, detail="season_number is only valid for media_type=series")

    media = await find_media(
        db, body.media_type,
        media_id=body.media_id, tmdb_id=body.tmdb_id, tvdb_id=body.tvdb_id,
        load_show=True,
    )

    from routers.media import get_user_tmdb_key
    from core import tmdb

    api_key = await get_user_tmdb_key(db, current_user.id)

    if not media and not body.tmdb_id and body.tvdb_id and body.media_type == MediaType.series:
        # A TheTVDB-only show has no TMDB record to fetch; its series row is
        # built from the local Show (created by its show page).
        media = await get_or_create_tvdb_series_media(db, body.tvdb_id)
        if media is None:
            raise HTTPException(status_code=404, detail="Show not found locally; open its show page first so it can be created from TheTVDB")

    if not media and not body.tmdb_id:
        raise HTTPException(status_code=404, detail="Media not found")
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
    elif body.tmdb_id and not media.adult and body.media_type in (MediaType.movie, MediaType.series):
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
        season_show_tmdb_id = body.tmdb_id or media.tmdb_id
        if season_show_tmdb_id:
            try:
                await tmdb.get_season(season_show_tmdb_id, body.season_number, api_key=api_key)
            except Exception:
                raise HTTPException(status_code=404, detail="Season not found")
        elif not media.tvdb_id:
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
    current_user: User = Depends(get_current_user_or_api_key),
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
