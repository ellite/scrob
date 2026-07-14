import asyncio
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, or_, select, desc, func, delete
from sqlalchemy.orm import selectinload
from db import get_db
from models.media import Media
from models.show import Show
from models.events import WatchEvent
from models.playback_session import PlaybackSession
from models.playback_progress import PlaybackProgress
from models.collection import Collection, CollectionFile
from models.base import MediaType, CollectionSource
from models.users import UserSettings
from models.connections import MediaServerConnection
from routers.media import enrich_with_state, get_user_tmdb_key, check_tmdb_key
from core.translations import get_user_metadata_language, get_media_translations, apply_media_translations

from dependencies import get_current_user
from models.users import User
import core.plex as plex_client
import core.jellyfin as jellyfin_client
import core.emby as emby_client
import core.trakt as trakt_client
import core.nuvio as nuvio_client

router = APIRouter()


async def _push_watch_state(
    db: AsyncSession,
    user_id: int,
    media_ids: list[int],
    watched: bool,
) -> None:
    """Fan-out watched/unwatched state to all connections with push_watched enabled."""
    if not media_ids:
        return

    conns_result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == user_id,
            MediaServerConnection.push_watched == True,
        )
    )
    connections = conns_result.scalars().all()

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    settings = settings_result.scalar_one_or_none()
    push_trakt = settings and settings.trakt_push_watched and settings.trakt_access_token

    tasks = []

    if connections:
        files_result = await db.execute(
            select(CollectionFile)
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .where(
                Collection.user_id == user_id,
                Collection.media_id.in_(media_ids),
            )
        )
        coll_files = files_result.scalars().all()

        conn_by_type: dict[str, list[MediaServerConnection]] = {}
        for conn in connections:
            conn_by_type.setdefault(conn.type, []).append(conn)

        for coll_file in coll_files:
            if not coll_file.source_id:
                continue
            source_type = coll_file.source.value if hasattr(coll_file.source, "value") else str(coll_file.source)
            for conn in conn_by_type.get(source_type, []):
                if coll_file.source == CollectionSource.plex:
                    if watched:
                        tasks.append(plex_client.mark_watched(conn.url, conn.token, coll_file.source_id))
                    else:
                        tasks.append(plex_client.mark_unwatched(conn.url, conn.token, coll_file.source_id))
                elif coll_file.source == CollectionSource.jellyfin:
                    if watched:
                        tasks.append(jellyfin_client.mark_watched(conn.url, conn.token, conn.server_user_id, coll_file.source_id))
                    else:
                        tasks.append(jellyfin_client.mark_unwatched(conn.url, conn.token, conn.server_user_id, coll_file.source_id))
                elif coll_file.source == CollectionSource.emby:
                    if watched:
                        tasks.append(emby_client.mark_watched(conn.url, conn.token, conn.server_user_id, coll_file.source_id))
                    else:
                        tasks.append(emby_client.mark_unwatched(conn.url, conn.token, conn.server_user_id, coll_file.source_id))

    push_simkl = settings and settings.simkl_push_watched and settings.simkl_access_token
    if push_simkl and settings.simkl_client_id:
        from core import simkl as simkl_client
        simkl_media_res = await db.execute(select(Media).where(Media.id.in_(media_ids)))
        simkl_media_items = simkl_media_res.scalars().all()
        for media in simkl_media_items:
            if not media.tmdb_id:
                continue
            if media.media_type == MediaType.movie:
                if watched:
                    tasks.append(simkl_client.add_movie_to_history(settings.simkl_client_id, settings.simkl_access_token, media.tmdb_id))
                else:
                    tasks.append(simkl_client.remove_movie_from_history(settings.simkl_client_id, settings.simkl_access_token, media.tmdb_id))
            elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                show_res = await db.execute(select(Show).where(Show.id == media.show_id))
                show = show_res.scalar_one_or_none()
                if show and show.tmdb_id:
                    if watched:
                        tasks.append(simkl_client.add_episode_to_history(settings.simkl_client_id, settings.simkl_access_token, show.tmdb_id, media.season_number, media.episode_number))
                    else:
                        tasks.append(simkl_client.remove_episode_from_history(settings.simkl_client_id, settings.simkl_access_token, show.tmdb_id, media.season_number, media.episode_number))

    if push_trakt and settings.trakt_client_id:
        media_res = await db.execute(
            select(Media).where(Media.id.in_(media_ids))
        )
        media_items = media_res.scalars().all()
        for media in media_items:
            if not media.tmdb_id:
                continue
            if media.media_type == MediaType.movie:
                if watched:
                    tasks.append(trakt_client.add_movie_to_history(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id))
                else:
                    tasks.append(trakt_client.remove_movie_from_history(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id))
            elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                show_res = await db.execute(select(Show).where(Show.id == media.show_id))
                show = show_res.scalar_one_or_none()
                if show and show.tmdb_id:
                    if watched:
                        tasks.append(trakt_client.add_episode_to_history(settings.trakt_client_id, settings.trakt_access_token, show.tmdb_id, media.season_number, media.episode_number))
                    else:
                        tasks.append(trakt_client.remove_episode_from_history(settings.trakt_client_id, settings.trakt_access_token, show.tmdb_id, media.season_number, media.episode_number))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    nuvio_connections = [conn for conn in connections if conn.type == "nuvio"]
    if nuvio_connections:
        media_result = await db.execute(select(Media).where(Media.id.in_(media_ids)))
        media_items = media_result.scalars().all()
        show_ids = {media.show_id for media in media_items if media.show_id is not None}
        shows_by_id: dict[int, Show] = {}
        if show_ids:
            show_result = await db.execute(select(Show).where(Show.id.in_(show_ids)))
            shows_by_id = {show.id: show for show in show_result.scalars().all()}
        from routers.sync import _ensure_nuvio_imdb_ids, _nuvio_watched_item

        api_key = await get_user_tmdb_key(db, user_id)
        await _ensure_nuvio_imdb_ids(media_items, shows_by_id, api_key)

        watched_at_by_media: dict[int, datetime] = {}
        if watched:
            event_result = await db.execute(
                select(WatchEvent.media_id, WatchEvent.watched_at)
                .where(
                    WatchEvent.user_id == user_id,
                    WatchEvent.media_id.in_(media_ids),
                    WatchEvent.completed == True,
                )
                .order_by(WatchEvent.watched_at.desc())
            )
            for media_id, watched_at in event_result.all():
                watched_at_by_media.setdefault(media_id, watched_at)

        nuvio_items: list[dict] = []
        nuvio_keys: list[dict] = []
        for media in media_items:
            payload = _nuvio_watched_item(
                media,
                watched_at_by_media.get(media.id, datetime.utcnow()),
                shows_by_id.get(media.show_id),
            )
            if not payload:
                continue
            key = {
                field: payload[field]
                for field in ("content_id", "season", "episode")
                if field in payload
            }
            if watched:
                nuvio_items.append(payload)
            else:
                nuvio_keys.append(key)

        for conn in nuvio_connections:
            try:
                profile_id = int(conn.server_user_id or "")
                if profile_id < 1 or profile_id > 6:
                    continue
                if watched and nuvio_items:
                    session = await nuvio_client.push_watched_items(
                        conn.url, conn.token, profile_id, nuvio_items
                    )
                elif not watched and nuvio_keys:
                    session = await nuvio_client.delete_watched_items(
                        conn.url, conn.token, profile_id, nuvio_keys
                    )
                else:
                    continue
                conn.token = session.refresh_token
            except Exception:
                continue
        await db.commit()


def format_event(event: WatchEvent | PlaybackProgress, media: Media) -> dict:
    # Handle both WatchEvent (history) and PlaybackProgress (continue watching)
    watched_at = getattr(event, "watched_at", None) or getattr(event, "updated_at", datetime.utcnow())
    
    data = {
        "id": event.id,
        "media": {
            "id": media.id,
            "tmdb_id": media.tmdb_id,
            "type": media.media_type,
            "title": media.title,
            "overview": media.overview,
            "poster_path": media.poster_path,
            "backdrop_path": media.backdrop_path,
            "release_date": media.release_date,
            "tmdb_rating": media.tmdb_rating,
            "user_rating": (media.tmdb_data or {}).get("user_rating"), # Placeholder, will be enriched
            "season_number": media.season_number,
            "episode_number": media.episode_number,
            "runtime": media.runtime,
            "tagline": media.tagline,
            "genres": (media.tmdb_data or {}).get("genres", []),
        },
        "user_id": event.user_id,
        "watched_at": watched_at.isoformat(),
        "progress_seconds": event.progress_seconds,
        "progress_percent": event.progress_percent,
        "completed": getattr(event, "completed", False),
        "play_count": getattr(event, "play_count", 1),
    }

    if media.media_type == MediaType.episode and media.show:
        data["media"]["show_title"] = media.show.title
        data["media"]["show_poster_path"] = media.show.poster_path
        data["media"]["show_tmdb_id"] = media.show.tmdb_id
        data["media"]["show_tvdb_id"] = media.show.tvdb_id

    return data


@router.get("")
async def get_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    offset = (page - 1) * page_size

    base_query = (
        select(func.count())
        .select_from(WatchEvent)
        .join(Media, Media.id == WatchEvent.media_id)
        .where(WatchEvent.user_id == current_user.id)
        .where(WatchEvent.completed == True)
    )
    if type and type in ("movie", "episode"):
        base_query = base_query.where(Media.media_type == type)

    total_result = await db.execute(base_query)
    total_count = total_result.scalar_one()
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    query = (
        select(WatchEvent, Media)
        .join(Media, Media.id == WatchEvent.media_id)
        .options(selectinload(WatchEvent.media).selectinload(Media.show))
        .where(WatchEvent.user_id == current_user.id)
        .where(WatchEvent.completed == True)
        .order_by(desc(WatchEvent.watched_at))
    )
    if type and type in ("movie", "episode"):
        query = query.where(Media.media_type == type)

    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    rows = result.all()
    
    events = [format_event(e, m) for e, m in rows]
    if events:
        await enrich_with_state(db, current_user.id, [e["media"] for e in events])
        lang = await get_user_metadata_language(db, current_user.id)
        if lang:
            media_ids = [e["media"]["id"] for e in events if e["media"].get("id")]
            translations = await get_media_translations(db, media_ids, lang)
            for event in events:
                t = translations.get(event["media"].get("id"))
                if t:
                    m = event["media"]
                    if t.get("title"): m["title"] = t["title"]
                    if t.get("overview"): m["overview"] = t["overview"]
                    if t.get("poster_path"): m["poster_path"] = t["poster_path"]

    return {
        "page": page,
        "page_size": page_size,
        "total_results": total_count,
        "total_pages": total_pages,
        "results": events,
    }


@router.get("/now-playing")
async def get_now_playing(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Active playback sessions for the current user."""
    result = await db.execute(
        select(PlaybackSession, Media)
        .join(Media, Media.id == PlaybackSession.media_id)
        .outerjoin(Show, Show.id == Media.show_id)
        .where(PlaybackSession.user_id == current_user.id)
        .order_by(desc(PlaybackSession.updated_at))
    )
    rows = result.all()
    sessions = []
    for session, media in rows:
        item: dict = {
            "session_key": session.session_key,
            "source": session.source,
            "state": session.state,
            "progress_percent": session.progress_percent,
            "progress_seconds": session.progress_seconds,
            "started_at": session.started_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "media": {
                "id": media.id,
                "tmdb_id": media.tmdb_id,
                "type": media.media_type,
                "title": media.title,
                "poster_path": media.poster_path,
                "backdrop_path": media.backdrop_path,
                "season_number": media.season_number,
                "episode_number": media.episode_number,
                "runtime": media.runtime,
            },
        }
        if media.media_type == MediaType.episode and media.show_id:
            show_result = await db.execute(select(Show).where(Show.id == media.show_id))
            show = show_result.scalar_one_or_none()
            if show:
                item["media"]["show_title"] = show.title
                item["media"]["show_tmdb_id"] = show.tmdb_id
                item["media"]["show_tvdb_id"] = show.tvdb_id
                item["media"]["show_poster_path"] = show.poster_path
                item["media"]["show_backdrop_path"] = show.backdrop_path
        elif media.media_type == MediaType.episode:
            hint = (media.tmdb_data or {}).get("show_title")
            if hint:
                item["media"]["show_title"] = hint
        sessions.append(item)
    return {"now_playing": sessions}


@router.delete("/sessions")
async def clear_now_playing_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete all active playback sessions for the current user."""
    await db.execute(
        delete(PlaybackSession).where(PlaybackSession.user_id == current_user.id)
    )
    await db.commit()
    return {"status": "ok"}


@router.get("/continue-watching")
async def get_continue_watching(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Items currently in progress."""
    result = await db.execute(
        select(PlaybackProgress, Media)
        .join(Media, Media.id == PlaybackProgress.media_id)
        .options(selectinload(PlaybackProgress.media).selectinload(Media.show))
        .where(PlaybackProgress.user_id == current_user.id)
        .order_by(desc(PlaybackProgress.updated_at))
        .limit(20)
    )
    rows = result.all()
    items = [format_event(e, m) for e, m in rows]
    if items:
        await enrich_with_state(db, current_user.id, [i["media"] for i in items])
        lang = await get_user_metadata_language(db, current_user.id)
        if lang:
            media_ids = [i["media"]["id"] for i in items if i["media"].get("id")]
            translations = await get_media_translations(db, media_ids, lang)
            for item in items:
                t = translations.get(item["media"].get("id"))
                if t:
                    m = item["media"]
                    if t.get("title"): m["title"] = t["title"]
                    if t.get("overview"): m["overview"] = t["overview"]
                    if t.get("poster_path"): m["poster_path"] = t["poster_path"]
    return {"continue_watching": items}


@router.delete("/continue-watching")
async def dismiss_continue_watching(
    media_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a single item from the continue-watching list."""
    await db.execute(
        delete(PlaybackProgress).where(
            PlaybackProgress.user_id == current_user.id,
            PlaybackProgress.media_id == media_id,
        )
    )
    await db.commit()
    return {"status": "ok"}


def _format_media_item(media: Media) -> dict:
    data = {
        "id": media.id,
        "tmdb_id": media.tmdb_id,
        "type": media.media_type,
        "title": media.title,
        "overview": media.overview,
        "poster_path": media.poster_path,
        "backdrop_path": media.backdrop_path,
        "release_date": media.release_date,
        "tmdb_rating": media.tmdb_rating,
        "season_number": media.season_number,
        "episode_number": media.episode_number,
        "runtime": media.runtime,
        "genres": (media.tmdb_data or {}).get("genres", []),
        "library": None,
        "in_library": False,
        "show_id": media.show_id,
    }
    if media.media_type == MediaType.episode and media.show:
        data["show_title"] = media.show.title
        data["show_poster_path"] = media.show.poster_path
        data["show_backdrop_path"] = media.show.backdrop_path
        data["show_tmdb_id"] = media.show.tmdb_id
        data["show_tvdb_id"] = media.show.tvdb_id
    return data


@router.get("/next-up")
async def get_next_up(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int | None = None,
    include_hidden: bool = Query(False),
):
    """Next unwatched episode for each show the user is actively watching."""
    # Step 1: Find the last watched / significantly-viewed episode per show,
    # and the most recent watch timestamp per show for final sorting.
    result = await db.execute(
        select(Media.show_id, Media.season_number, Media.episode_number, WatchEvent.watched_at)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == current_user.id,
            Media.media_type == MediaType.episode,
            Media.show_id.isnot(None),
            or_(WatchEvent.completed == True, WatchEvent.progress_percent >= 0.5),
        )
        .order_by(Media.show_id, desc(Media.season_number), desc(Media.episode_number))
    )
    rows = result.all()

    # Keep only the furthest episode per show, and the most recent watched_at per show.
    last_per_show: dict[int, tuple[int, int]] = {}
    last_watched_at: dict[int, object] = {}
    for show_id, season, episode, watched_at in rows:
        if show_id not in last_per_show:
            last_per_show[show_id] = (season, episode)
        if show_id not in last_watched_at or (watched_at and watched_at > last_watched_at[show_id]):
            last_watched_at[show_id] = watched_at

    if not last_per_show:
        return {"next_up": []}

    # Step 2: Candidate next episodes (anything after the last watched one, per show)
    show_filters = [
        and_(
            Media.show_id == show_id,
            or_(
                Media.season_number > season,
                and_(Media.season_number == season, Media.episode_number > episode),
            ),
        )
        for show_id, (season, episode) in last_per_show.items()
    ]

    candidates_result = await db.execute(
        select(Media)
        .options(selectinload(Media.show))
        .where(Media.media_type == MediaType.episode, or_(*show_filters))
        .order_by(Media.show_id, Media.season_number, Media.episode_number)
    )
    candidates = candidates_result.scalars().all()

    # Take only the immediately next episode per show
    next_per_show: dict[int, Media] = {}
    for media in candidates:
        if media.show_id not in next_per_show:
            next_per_show[media.show_id] = media

    if not next_per_show:
        return {"next_up": []}

    # Remove episodes the user has already completed
    completed_result = await db.execute(
        select(WatchEvent.media_id)
        .where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.completed == True,
            WatchEvent.media_id.in_([m.id for m in next_per_show.values()]),
        )
    )
    completed_ids = {row[0] for row in completed_result.all()}

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    hidden_set = set(settings.next_up_hidden_shows or []) if settings else set()

    next_up = [
        m for m in next_per_show.values()
        if m.id not in completed_ids and (include_hidden or m.show_id not in hidden_set)
    ]
    next_up.sort(key=lambda m: last_watched_at.get(m.show_id) or datetime.min, reverse=True)
    if limit is not None:
        next_up = next_up[:limit]

    items = [_format_media_item(m) for m in next_up]
    for item in items:
        item["next_up_hidden"] = item.get("show_id") in hidden_set
    if items:
        await enrich_with_state(db, current_user.id, items)
        lang = await get_user_metadata_language(db, current_user.id)
        if lang:
            media_ids = [i["id"] for i in items if i.get("id")]
            translations = await get_media_translations(db, media_ids, lang)
            apply_media_translations(items, translations)

    return {"next_up": items}


import schemas
from core import tmdb
from core.enrichment import enrich_media
from datetime import datetime
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm.attributes import flag_modified


class NextUpHideRequest(BaseModel):
    show_id: int


@router.post("/next-up/hide")
async def hide_next_up_show(
    body: NextUpHideRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found")
    hidden = list(settings.next_up_hidden_shows or [])
    if body.show_id not in hidden:
        hidden.append(body.show_id)
        settings.next_up_hidden_shows = hidden
        flag_modified(settings, "next_up_hidden_shows")
        await db.commit()
    return {"status": "ok"}


@router.delete("/next-up/hide")
async def unhide_next_up_show(
    show_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if settings:
        hidden = list(settings.next_up_hidden_shows or [])
        if show_id in hidden:
            hidden.remove(show_id)
            settings.next_up_hidden_shows = hidden
            flag_modified(settings, "next_up_hidden_shows")
            await db.commit()
    return {"status": "ok"}


class SeasonWatchRequest(BaseModel):
    series_tmdb_id: int
    season_number: int


class ShowWatchRequest(BaseModel):
    series_tmdb_id: int


@router.post("", response_model=dict)
async def mark_as_watched(
    event_in: schemas.WatchEventCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. Check if Media exists locally
    media = None

    if event_in.media_type == MediaType.episode and event_in.series_tmdb_id and event_in.season_number is not None and event_in.episode_number is not None:
        # For episodes, prefer looking up by show + season + episode since tmdb_id
        # may not be set on Media records created via TVDB or webhook paths.
        show_result = await db.execute(select(Show).where(Show.tmdb_id == event_in.series_tmdb_id))
        show = show_result.scalar_one_or_none()
        if show:
            ep_result = await db.execute(
                select(Media)
                .where(Media.media_type == MediaType.episode)
                .where(Media.show_id == show.id)
                .where(Media.season_number == event_in.season_number)
                .where(Media.episode_number == event_in.episode_number)
            )
            media = ep_result.scalars().first()

    if not media:
        result = await db.execute(
            select(Media).where(Media.tmdb_id == event_in.tmdb_id, Media.media_type == event_in.media_type)
        )
        media = result.scalar_one_or_none()

    # 2. If not, create Media record from TMDB
    if not media:
        from routers.media import get_user_tmdb_key

        api_key = await get_user_tmdb_key(db, current_user.id)

        try:
            if event_in.media_type == MediaType.movie:
                data = await tmdb.get_movie(event_in.tmdb_id, api_key=api_key)
                media = Media(
                    tmdb_id=event_in.tmdb_id, media_type=event_in.media_type, title=data.get("title")
                )
                db.add(media)
                await db.flush()
                await enrich_media(media, api_key=api_key)
            elif event_in.media_type == MediaType.episode and event_in.series_tmdb_id and event_in.season_number is not None and event_in.episode_number is not None:
                ep_data = await tmdb.get_episode(
                    event_in.series_tmdb_id, event_in.season_number, event_in.episode_number, api_key=api_key
                )
                show_result = await db.execute(select(Show).where(Show.tmdb_id == event_in.series_tmdb_id))
                show = show_result.scalar_one_or_none()
                media = Media(
                    tmdb_id=ep_data.get("id"),
                    media_type=MediaType.episode,
                    title=ep_data.get("name"),
                    season_number=event_in.season_number,
                    episode_number=event_in.episode_number,
                    show_id=show.id if show else None,
                )
                db.add(media)
                await db.flush()
                await enrich_media(media, api_key=api_key, series_tmdb_id=event_in.series_tmdb_id)
            else:
                raise HTTPException(status_code=404, detail="Episode context required (series_tmdb_id, season_number, episode_number)")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"TMDB Media not found: {e}")

    # 3. Create WatchEvent
    event = WatchEvent(
        user_id=current_user.id,
        media_id=media.id,
        watched_at=(event_in.watched_at.replace(tzinfo=None) if event_in.watched_at else datetime.utcnow()),
        completed=event_in.completed,
        play_count=1,
        progress_percent=1.0 if event_in.completed else 0.0,
    )
    db.add(event)
    if event_in.completed:
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == current_user.id,
                PlaybackProgress.media_id == media.id,
            )
        )
    await db.commit()

    # 4. Push to media servers if outbound push is enabled
    if event_in.completed:
        await _push_watch_state(db, current_user.id, [media.id], watched=True)

    return {"status": "ok", "message": f"Marked {media.title} as watched"}


@router.get("/item-events")
async def get_item_events(
    tmdb_id: int = Query(...),
    media_type: MediaType = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all completed watch events for a specific movie or episode."""
    query = (
        select(WatchEvent)
        .join(Media, Media.id == WatchEvent.media_id)
        .where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.completed == True,
            Media.tmdb_id == tmdb_id,
            Media.media_type == media_type,
        )
        .order_by(desc(WatchEvent.watched_at))
    )
    result = await db.execute(query)
    events = result.scalars().all()
    return [{"id": e.id, "watched_at": e.watched_at.isoformat()} for e in events]


@router.delete("/event/{event_id}")
async def delete_single_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a single watch event by its ID."""
    result = await db.execute(
        select(WatchEvent).where(
            WatchEvent.id == event_id,
            WatchEvent.user_id == current_user.id,
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    media_id = event.media_id
    await db.execute(
        delete(WatchEvent).where(
            WatchEvent.id == event_id,
            WatchEvent.user_id == current_user.id,
        )
    )
    await db.commit()

    # Only push "unwatched" to connected services if no events remain for this media
    remaining = await db.execute(
        select(func.count()).where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.media_id == media_id,
        )
    )
    if remaining.scalar() == 0:
        await _push_watch_state(db, current_user.id, [media_id], watched=False)

    return {"status": "ok"}


@router.delete("")
async def clear_history(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await db.execute(delete(WatchEvent).where(WatchEvent.user_id == current_user.id))
    await db.commit()
    return {"status": "ok", "message": "Watch history cleared"}


@router.delete("/item")
async def unwatch_item(
    tmdb_id: int | None = Query(None),
    media_id: int | None = Query(None, alias="id"),
    media_type: MediaType = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove all watch events for a specific item."""
    if not tmdb_id and not media_id:
        raise HTTPException(status_code=400, detail="Either tmdb_id or id is required")

    if tmdb_id:
        media_q = await db.execute(
            select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == media_type)
        )
    else:
        media_q = await db.execute(
            select(Media).where(Media.id == media_id, Media.media_type == media_type)
        )
    
    media = media_q.scalar_one_or_none()
    if not media:
        return {"status": "ok", "count": 0}
    await db.execute(
        delete(WatchEvent).where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.media_id == media.id,
        )
    )
    await db.commit()
    await _push_watch_state(db, current_user.id, [media.id], watched=False)
    return {"status": "ok"}


@router.post("/season")
async def mark_season_watched(
    body: SeasonWatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark all aired episodes of a season as watched, fetching from TMDB if needed."""
    # 1. Ensure show exists
    show_q = await db.execute(select(Show).where(Show.tmdb_id == body.series_tmdb_id))
    show = show_q.scalar_one_or_none()
    
    api_key = await get_user_tmdb_key(db, current_user.id)
    if not show:
        if not check_tmdb_key(api_key):
            raise HTTPException(status_code=404, detail="Show not found and TMDB key not configured")
        data = await tmdb.get_show(body.series_tmdb_id, api_key=api_key)
        show = Show(
            tmdb_id=body.series_tmdb_id,
            title=data.get("name") or "Unknown",
            poster_path=tmdb.poster_url(data.get("poster_path")),
            backdrop_path=tmdb.poster_url(data.get("backdrop_path"), size="w1280"),
            tmdb_rating=data.get("vote_average"),
            status=data.get("status"),
            first_air_date=data.get("first_air_date"),
            tmdb_data={
                "genres": [g["name"] for g in data.get("genres", [])],
                "seasons": [
                    {
                        "season_number": s["season_number"],
                        "episode_count": s["episode_count"],
                        "name": s["name"],
                    } for s in data.get("seasons", [])
                ]
            }
        )
        db.add(show)
        await db.flush()

    # 2. Fetch season episodes from TMDB to ensure we know about all of them
    try:
        season_data = await tmdb.get_season(body.series_tmdb_id, body.season_number, api_key=api_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Season not found: {e}")

    # 3. Ensure Media rows exist for all aired episodes in this season
    now = datetime.utcnow()
    today = now.date()
    
    # Get existing episodes for this season
    existing_q = await db.execute(
        select(Media).where(
            Media.show_id == show.id,
            Media.media_type == MediaType.episode,
            Media.season_number == body.season_number
        )
    )
    existing_map = {m.episode_number: m for m in existing_q.scalars().all()}
    
    all_season_episodes = []
    for ep in season_data.get("episodes", []):
        air_date_str = ep.get("air_date")
        if not air_date_str: continue
        try:
            air_date = datetime.strptime(air_date_str, "%Y-%m-%d").date()
            if air_date > today: continue # Skip unaired
        except Exception: continue
        
        ep_num = ep["episode_number"]
        if ep_num in existing_map:
            all_season_episodes.append(existing_map[ep_num])
        else:
            new_ep = Media(
                show_id=show.id,
                tmdb_id=ep["id"],
                media_type=MediaType.episode,
                title=ep.get("name") or f"Episode {ep_num}",
                season_number=body.season_number,
                episode_number=ep_num,
                poster_path=tmdb.poster_url(ep.get("still_path"), size="w500"),
                release_date=air_date_str,
                tmdb_rating=ep.get("vote_average"),
            )
            db.add(new_ep)
            all_season_episodes.append(new_ep)
    
    await db.flush() # Get IDs for new episodes
    
    # 4. Mark all as watched
    if not all_season_episodes:
        return {"status": "ok", "count": 0}

    already_q = await db.execute(
        select(WatchEvent.media_id).where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.media_id.in_([ep.id for ep in all_season_episodes]),
            WatchEvent.completed == True
        )
    )
    already_watched = {r[0] for r in already_q.all()}
    
    newly_watched = []
    for ep in all_season_episodes:
        if ep.id not in already_watched:
            db.add(WatchEvent(
                user_id=current_user.id,
                media_id=ep.id,
                watched_at=now,
                completed=True,
                play_count=1,
                progress_percent=1.0,
            ))
            newly_watched.append(ep.id)
            
    if newly_watched:
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == current_user.id,
                PlaybackProgress.media_id.in_(newly_watched),
            )
        )
    await db.commit()
    await _push_watch_state(db, current_user.id, newly_watched, watched=True)
    return {"status": "ok", "count": len(newly_watched)}


@router.delete("/season")
async def unwatch_season(
    series_tmdb_id: int = Query(...),
    season_number: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove all watch events for a season."""
    show_q = await db.execute(select(Show).where(Show.tmdb_id == series_tmdb_id))
    show = show_q.scalar_one_or_none()
    if not show:
        return {"status": "ok", "count": 0}

    episodes_q = await db.execute(
        select(Media.id).where(
            Media.show_id == show.id,
            Media.media_type == MediaType.episode,
            Media.season_number == season_number,
        )
    )
    episode_ids = [r[0] for r in episodes_q.all()]
    if not episode_ids:
        return {"status": "ok", "count": 0}

    result = await db.execute(
        delete(WatchEvent).where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.media_id.in_(episode_ids),
        )
    )
    await db.commit()
    await _push_watch_state(db, current_user.id, episode_ids, watched=False)
    return {"status": "ok", "count": result.rowcount}


@router.post("/show-all")
async def mark_show_watched(
    body: ShowWatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark all aired episodes of all seasons as watched."""
    # 1. Ensure show exists and get its metadata
    show_q = await db.execute(select(Show).where(Show.tmdb_id == body.series_tmdb_id))
    show = show_q.scalar_one_or_none()
    
    api_key = await get_user_tmdb_key(db, current_user.id)
    if not show:
        if not check_tmdb_key(api_key):
            raise HTTPException(status_code=404, detail="Show not found and TMDB key not configured")
        data = await tmdb.get_show(body.series_tmdb_id, api_key=api_key)
        show = Show(
            tmdb_id=body.series_tmdb_id,
            title=data.get("name") or "Unknown",
            poster_path=tmdb.poster_url(data.get("poster_path")),
            backdrop_path=tmdb.poster_url(data.get("backdrop_path"), size="w1280"),
            tmdb_rating=data.get("vote_average"),
            status=data.get("status"),
            first_air_date=data.get("first_air_date"),
            tmdb_data={
                "genres": [g["name"] for g in data.get("genres", [])],
                "seasons": [
                    {
                        "season_number": s["season_number"],
                        "episode_count": s["episode_count"],
                        "name": s["name"],
                    } for s in data.get("seasons", [])
                ]
            }
        )
        db.add(show)
        await db.flush()
    else:
        # We need TMDB data for season/episode counts
        if not show.tmdb_data or "seasons" not in show.tmdb_data:
            data = await tmdb.get_show(body.series_tmdb_id, api_key=api_key)
            show.tmdb_data = {
                "genres": [g["name"] for g in data.get("genres", [])],
                "seasons": [
                    {
                        "season_number": s["season_number"],
                        "episode_count": s["episode_count"],
                        "name": s["name"],
                    } for s in data.get("seasons", [])
                ]
            }
            await db.flush()

    # 2. For each season, fetch episodes and ensure they exist + mark watched
    seasons = [s["season_number"] for s in show.tmdb_data["seasons"] if s["season_number"] > 0]
    all_newly_watched_ids = []
    
    now = datetime.utcnow()
    today = now.date()

    for sn in seasons:
        try:
            season_data = await tmdb.get_season(body.series_tmdb_id, sn, api_key=api_key)
        except Exception: continue # Skip failed seasons

        existing_q = await db.execute(
            select(Media).where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number == sn
            )
        )
        existing_map = {m.episode_number: m for m in existing_q.scalars().all()}
        
        season_eps_to_watch = []
        for ep in season_data.get("episodes", []):
            air_date_str = ep.get("air_date")
            if not air_date_str: continue
            try:
                air_date = datetime.strptime(air_date_str, "%Y-%m-%d").date()
                if air_date > today: continue
            except Exception: continue
            
            ep_num = ep["episode_number"]
            if ep_num in existing_map:
                season_eps_to_watch.append(existing_map[ep_num])
            else:
                new_ep = Media(
                    show_id=show.id,
                    tmdb_id=ep["id"],
                    media_type=MediaType.episode,
                    title=ep.get("name") or f"Episode {ep_num}",
                    season_number=sn,
                    episode_number=ep_num,
                    poster_path=tmdb.poster_url(ep.get("still_path"), size="w500"),
                    release_date=air_date_str,
                    tmdb_rating=ep.get("vote_average"),
                )
                db.add(new_ep)
                season_eps_to_watch.append(new_ep)
        
        await db.flush()
        
        if not season_eps_to_watch: continue

        already_q = await db.execute(
            select(WatchEvent.media_id).where(
                WatchEvent.user_id == current_user.id,
                WatchEvent.media_id.in_([ep.id for ep in season_eps_to_watch]),
                WatchEvent.completed == True
            )
        )
        already_watched = {r[0] for r in already_q.all()}
        
        for ep in season_eps_to_watch:
            if ep.id not in already_watched:
                db.add(WatchEvent(
                    user_id=current_user.id,
                    media_id=ep.id,
                    watched_at=now,
                    completed=True,
                    play_count=1,
                    progress_percent=1.0,
                ))
                all_newly_watched_ids.append(ep.id)

    if all_newly_watched_ids:
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == current_user.id,
                PlaybackProgress.media_id.in_(all_newly_watched_ids),
            )
        )
    await db.commit()
    await _push_watch_state(db, current_user.id, all_newly_watched_ids, watched=True)
    return {"status": "ok", "count": len(all_newly_watched_ids)}


@router.delete("/show-all")
async def unwatch_show(
    series_tmdb_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove all watch events for all episodes of a show."""
    show_q = await db.execute(select(Show).where(Show.tmdb_id == series_tmdb_id))
    show = show_q.scalar_one_or_none()
    if not show:
        return {"status": "ok", "count": 0}

    episodes_q = await db.execute(
        select(Media.id).where(
            Media.show_id == show.id,
            Media.media_type == MediaType.episode,
        )
    )
    episode_ids = [r[0] for r in episodes_q.all()]
    if not episode_ids:
        return {"status": "ok", "count": 0}

    result = await db.execute(
        delete(WatchEvent).where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.media_id.in_(episode_ids),
        )
    )
    await db.commit()
    await _push_watch_state(db, current_user.id, episode_ids, watched=False)
    return {"status": "ok", "count": result.rowcount}


# ---------------------------------------------------------------------------
# Manual scrobble session endpoints
# ---------------------------------------------------------------------------

async def _get_or_create_media_for_session(
    db: AsyncSession,
    body: schemas.ManualSessionStart,
    user_id: int,
) -> Media:
    # Prefer direct media_id lookup (used for TVDB-only episodes with no tmdb_id)
    if body.media_id:
        result = await db.execute(select(Media).where(Media.id == body.media_id))
        media = result.scalar_one_or_none()
        if media:
            return media

    if body.tmdb_id:
        result = await db.execute(
            select(Media).where(Media.tmdb_id == body.tmdb_id, Media.media_type == body.media_type)
        )
        media = result.scalar_one_or_none()
        if media:
            return media

    api_key = await get_user_tmdb_key(db, user_id)

    if body.media_type == MediaType.movie:
        if not body.tmdb_id:
            raise HTTPException(status_code=400, detail="tmdb_id required for movies")
        if not check_tmdb_key(api_key):
            raise HTTPException(status_code=404, detail="Movie not in library and TMDB key not configured")
        try:
            data = await tmdb.get_movie(body.tmdb_id, api_key=api_key)
            title = data.get("title") or body.title or "Unknown"
        except Exception:
            title = body.title or "Unknown"
        media = Media(tmdb_id=body.tmdb_id, media_type=body.media_type, title=title)
        db.add(media)
        await db.flush()
        try:
            await enrich_media(media, api_key=api_key)
        except Exception:
            pass
    else:
        # Episode: create a minimal row from request data
        media = Media(
            tmdb_id=body.tmdb_id,
            media_type=body.media_type,
            title=body.title or "Unknown",
            runtime=body.runtime,
            season_number=body.season_number,
            episode_number=body.episode_number,
        )
        if body.show_tmdb_id:
            show_q = await db.execute(select(Show).where(Show.tmdb_id == body.show_tmdb_id))
            show = show_q.scalar_one_or_none()
            if show:
                media.show_id = show.id
        db.add(media)
        await db.flush()

    return media


@router.post("/session/start")
async def start_manual_session(
    body: schemas.ManualSessionStart,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start a manual scrobble session for any movie or episode."""
    media = await _get_or_create_media_for_session(db, body, current_user.id)

    if media.runtime is None and body.runtime:
        media.runtime = body.runtime

    session_key = f"manual-{current_user.id}-{media.id}"

    await db.execute(delete(PlaybackSession).where(PlaybackSession.session_key == session_key))
    session = PlaybackSession(
        user_id=current_user.id,
        media_id=media.id,
        session_key=session_key,
        source="manual",
        state="playing",
        progress_seconds=0,
        progress_percent=0.0,
    )
    db.add(session)
    await db.commit()

    return {"session_key": session_key, "media_id": media.id, "runtime": media.runtime}


@router.patch("/session/{session_key}")
async def update_manual_session(
    session_key: str,
    body: schemas.ManualSessionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Heartbeat / pause / resume for a manual session."""
    result = await db.execute(
        select(PlaybackSession).where(
            PlaybackSession.session_key == session_key,
            PlaybackSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    media_q = await db.execute(select(Media).where(Media.id == session.media_id))
    media = media_q.scalar_one_or_none()

    runtime_seconds = (media.runtime * 60) if (media and media.runtime) else 0
    progress_pct = (body.progress_seconds / runtime_seconds) if runtime_seconds > 0 else 0.0
    progress_pct = min(1.0, max(0.0, progress_pct))

    session.progress_seconds = body.progress_seconds
    session.progress_percent = progress_pct
    if body.state in ("playing", "paused"):
        session.state = body.state
    session.updated_at = datetime.utcnow()

    if 0.05 <= progress_pct < 0.90:
        prog_q = await db.execute(
            select(PlaybackProgress).where(
                PlaybackProgress.user_id == current_user.id,
                PlaybackProgress.media_id == session.media_id,
            )
        )
        prog = prog_q.scalar_one_or_none()
        if prog:
            prog.progress_seconds = body.progress_seconds
            prog.progress_percent = progress_pct
        else:
            db.add(PlaybackProgress(
                user_id=current_user.id,
                media_id=session.media_id,
                progress_seconds=body.progress_seconds,
                progress_percent=progress_pct,
            ))

    await db.commit()
    return {"status": "ok"}


@router.delete("/session/{session_key}")
async def stop_manual_session(
    session_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stop and discard a manual session without marking as watched."""
    result = await db.execute(
        select(PlaybackSession).where(
            PlaybackSession.session_key == session_key,
            PlaybackSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    media_id = session.media_id
    await db.execute(delete(PlaybackSession).where(PlaybackSession.session_key == session_key))
    await db.execute(
        delete(PlaybackProgress).where(
            PlaybackProgress.user_id == current_user.id,
            PlaybackProgress.media_id == media_id,
        )
    )
    await db.commit()
    return {"status": "ok"}


async def auto_complete_manual_sessions(db: AsyncSession) -> None:
    """Complete any manual sessions where enough time has elapsed since the last heartbeat."""
    now = datetime.utcnow()
    result = await db.execute(
        select(PlaybackSession, Media)
        .join(Media, Media.id == PlaybackSession.media_id)
        .where(PlaybackSession.source == "manual", PlaybackSession.state == "playing")
    )
    completed: list[tuple[int, int]] = []  # (user_id, media_id)
    for session, media in result.all():
        runtime_seconds = (media.runtime or 0) * 60
        if runtime_seconds <= 0:
            continue
        elapsed = session.progress_seconds + (now - session.updated_at).total_seconds()
        if elapsed < runtime_seconds:
            continue
        await db.execute(delete(PlaybackSession).where(PlaybackSession.id == session.id))
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == session.user_id,
                PlaybackProgress.media_id == session.media_id,
            )
        )
        db.add(WatchEvent(
            user_id=session.user_id,
            media_id=session.media_id,
            watched_at=now,
            completed=True,
            play_count=1,
            progress_percent=1.0,
        ))
        completed.append((session.user_id, session.media_id))
    if completed:
        await db.commit()
        for user_id, media_id in completed:
            await _push_watch_state(db, user_id, [media_id], watched=True)


@router.post("/session/{session_key}/complete")
async def complete_manual_session(
    session_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark as fully watched and end the session."""
    result = await db.execute(
        select(PlaybackSession).where(
            PlaybackSession.session_key == session_key,
            PlaybackSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    media_id = session.media_id
    await db.execute(delete(PlaybackSession).where(PlaybackSession.session_key == session_key))
    await db.execute(
        delete(PlaybackProgress).where(
            PlaybackProgress.user_id == current_user.id,
            PlaybackProgress.media_id == media_id,
        )
    )

    db.add(WatchEvent(
        user_id=current_user.id,
        media_id=media_id,
        watched_at=datetime.utcnow(),
        completed=True,
        play_count=1,
        progress_percent=1.0,
    ))
    await db.commit()

    await _push_watch_state(db, current_user.id, [media_id], watched=True)
    return {"status": "ok"}
