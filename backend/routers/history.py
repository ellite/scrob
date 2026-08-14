import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, or_, select, desc, func, delete, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload, aliased
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
from models.episode_order import EpisodeOrderMapping
from models.rewatch import ShowRewatch, RewatchProgress
from routers.media import enrich_with_state, get_user_tmdb_key, check_tmdb_key
from core.translations import get_user_metadata_language, get_media_translations, apply_media_translations
from core.rewatch import get_active_rewatch, record_rewatch_progress, get_already_watched_for_bulk_mark, capped_season_episode_counts
from core.enrichment import create_media_safely

from dependencies import get_current_user, get_current_user_or_api_key
from models.users import User
import core.plex as plex_client
import core.jellyfin as jellyfin_client
import core.emby as emby_client
import core.trakt as trakt_client
import core.nuvio as nuvio_client

router = APIRouter()
logger = logging.getLogger(__name__)


async def _push_watch_state(
    db: AsyncSession,
    user_id: int,
    media_ids: list[int],
    watched: bool,
    watched_at_by_media: dict[int, datetime | None] | None = None,
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
    push_mdblist = settings and settings.mdblist_push_watched and settings.mdblist_api_key

    resolved_watched_at: dict[int, datetime | None] = {}
    if watched:
        if watched_at_by_media is not None:
            resolved_watched_at = watched_at_by_media
        else:
            from routers.sync import _latest_watched_at
            resolved_watched_at = await _latest_watched_at(db, user_id, media_ids)

    # Each entry is (label, coroutine) so a failure can be logged with which
    # provider/connection it came from — asyncio.gather(return_exceptions=True)
    # would otherwise swallow errors here silently.
    tasks: list[tuple[str, Any]] = []

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
                    label = f"plex connection {conn.id}"
                    if watched:
                        tasks.append((label, plex_client.mark_watched(conn.url, conn.token, coll_file.source_id)))
                    else:
                        tasks.append((label, plex_client.mark_unwatched(conn.url, conn.token, coll_file.source_id)))
                elif coll_file.source == CollectionSource.jellyfin:
                    label = f"jellyfin connection {conn.id}"
                    if watched:
                        tasks.append((label, jellyfin_client.mark_watched(conn.url, conn.token, conn.server_user_id, coll_file.source_id)))
                    else:
                        tasks.append((label, jellyfin_client.mark_unwatched(conn.url, conn.token, conn.server_user_id, coll_file.source_id)))
                elif coll_file.source == CollectionSource.emby:
                    label = f"emby connection {conn.id}"
                    if watched:
                        tasks.append((label, emby_client.mark_watched(conn.url, conn.token, conn.server_user_id, coll_file.source_id)))
                    else:
                        tasks.append((label, emby_client.mark_unwatched(conn.url, conn.token, conn.server_user_id, coll_file.source_id)))

    push_simkl = settings and settings.simkl_push_watched and settings.simkl_access_token
    if push_simkl and settings.simkl_client_id:
        from core import simkl as simkl_client
        simkl_media_res = await db.execute(select(Media).where(Media.id.in_(media_ids)))
        simkl_media_items = simkl_media_res.scalars().all()
        for media in simkl_media_items:
            if not media.tmdb_id or is_unmapped_tvdb_episode(media):
                continue
            if media.media_type == MediaType.movie:
                if watched:
                    watched_at = resolved_watched_at.get(media.id)
                    if watched_at is not None:
                        tasks.append((f"simkl add movie {media.tmdb_id}", simkl_client.add_movie_to_history(settings.simkl_client_id, settings.simkl_access_token, media.tmdb_id, watched_at)))
                else:
                    tasks.append((f"simkl remove movie {media.tmdb_id}", simkl_client.remove_movie_from_history(settings.simkl_client_id, settings.simkl_access_token, media.tmdb_id)))
            elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                show_res = await db.execute(select(Show).where(Show.id == media.show_id))
                show = show_res.scalar_one_or_none()
                if show and show.tmdb_id:
                    if watched:
                        watched_at = resolved_watched_at.get(media.id)
                        if watched_at is not None:
                            tasks.append((f"simkl add episode {show.tmdb_id} S{media.season_number}E{media.episode_number}", simkl_client.add_episode_to_history(settings.simkl_client_id, settings.simkl_access_token, show.tmdb_id, media.season_number, media.episode_number, watched_at)))
                    else:
                        tasks.append((f"simkl remove episode {show.tmdb_id} S{media.season_number}E{media.episode_number}", simkl_client.remove_episode_from_history(settings.simkl_client_id, settings.simkl_access_token, show.tmdb_id, media.season_number, media.episode_number)))

    if push_trakt and settings.trakt_client_id:
        media_res = await db.execute(
            select(Media).where(Media.id.in_(media_ids))
        )
        media_items = media_res.scalars().all()
        for media in media_items:
            if not media.tmdb_id or is_unmapped_tvdb_episode(media):
                continue
            if media.media_type == MediaType.movie:
                if watched:
                    tasks.append((f"trakt add movie {media.tmdb_id}", trakt_client.add_movie_to_history(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id, resolved_watched_at.get(media.id))))
                else:
                    tasks.append((f"trakt remove movie {media.tmdb_id}", trakt_client.remove_movie_from_history(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id)))
            elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                show_res = await db.execute(select(Show).where(Show.id == media.show_id))
                show = show_res.scalar_one_or_none()
                if show and show.tmdb_id:
                    if watched:
                        tasks.append((f"trakt add episode {show.tmdb_id} S{media.season_number}E{media.episode_number}", trakt_client.add_episode_to_history(settings.trakt_client_id, settings.trakt_access_token, show.tmdb_id, media.season_number, media.episode_number, resolved_watched_at.get(media.id))))
                    else:
                        tasks.append((f"trakt remove episode {show.tmdb_id} S{media.season_number}E{media.episode_number}", trakt_client.remove_episode_from_history(settings.trakt_client_id, settings.trakt_access_token, show.tmdb_id, media.season_number, media.episode_number)))

    if push_mdblist:
        from core import mdblist as mdblist_client
        from routers.mdblist import _empty_payload, _merge_show_entries, _payload_item

        mdblist_payload = _empty_payload()
        media_result = await db.execute(select(Media).where(Media.id.in_(media_ids)))
        media_list = media_result.scalars().all()
        mdblist_show_ids = {m.show_id for m in media_list if m.media_type == MediaType.episode and m.show_id}
        mdblist_shows_by_id: dict[int, Show] = {}
        if mdblist_show_ids:
            shows_result = await db.execute(select(Show).where(Show.id.in_(mdblist_show_ids)))
            mdblist_shows_by_id = {s.id: s for s in shows_result.scalars().all()}
        for media in media_list:
            if is_unmapped_tvdb_episode(media):
                continue
            show = mdblist_shows_by_id.get(media.show_id)
            item = (
                _payload_item(media, show=show, watched_at=resolved_watched_at.get(media.id, datetime.utcnow()))
                if watched
                else _payload_item(media, show=show)
            )
            if item:
                mdblist_payload[item[0]].append(item[1])
        mdblist_payload["shows"] = _merge_show_entries(mdblist_payload["shows"])
        # MDBList's /sync/watched/remove has no per-item removal feed — it bumps
        # a removal timestamp on /sync/last_activities and expects clients to
        # re-fetch the whole watched snapshot rather than confirming per item,
        # so removal on their end can lag visibly behind this call returning.
        operation = mdblist_client.push_watched if watched else mdblist_client.remove_watched
        tasks.append((f"mdblist {'push' if watched else 'remove'} watched", operation(settings.mdblist_api_key, mdblist_payload)))

    if tasks:
        results = await asyncio.gather(*(coro for _, coro in tasks), return_exceptions=True)
        for (label, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.exception("Failed to push watch state to %s", label, exc_info=result)

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

        nuvio_items: list[dict] = []
        nuvio_keys: list[dict] = []
        for media in media_items:
            if is_unmapped_tvdb_episode(media):
                continue
            payload = _nuvio_watched_item(
                media,
                resolved_watched_at.get(media.id) if watched else datetime.utcnow(),
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
                profile_id = nuvio_client.parse_profile_id(conn.server_user_id)

                async def _persist_refresh(session: nuvio_client.NuvioSession, conn=conn) -> None:
                    conn.token = session.refresh_token
                    await db.commit()

                async with nuvio_client.connection_lock(conn.id):
                    if watched and nuvio_items:
                        await nuvio_client.push_watched_items(
                            conn.url, conn.token, profile_id, nuvio_items, on_refresh=_persist_refresh
                        )
                    elif not watched and nuvio_keys:
                        await nuvio_client.delete_watched_items(
                            conn.url, conn.token, profile_id, nuvio_keys, on_refresh=_persist_refresh
                        )
                    else:
                        continue
            except Exception:
                logger.exception("Failed to push watch state to Nuvio connection %s", conn.id)
                continue
        await db.commit()

    stremio_connections = [conn for conn in connections if conn.type == "stremio"]
    if stremio_connections:
        from routers.sync import _get_effective_tmdb_key, _push_stremio_connection

        api_key = await _get_effective_tmdb_key(db, settings)
        # Exclude episodes enriched from TVDB (no real TMDB counterpart, see
        # #101) — their tmdb_id is a disguised TVDB episode id, not safe to
        # resolve against Stremio's TMDB/IMDb-keyed content ids.
        stremio_media_result = await db.execute(select(Media).where(Media.id.in_(media_ids)))
        stremio_eligible_ids = {
            media.id for media in stremio_media_result.scalars().all()
            if not is_unmapped_tvdb_episode(media)
        }
        watch_overrides = {media_id: watched for media_id in media_ids if media_id in stremio_eligible_ids}
        for conn in stremio_connections:
            try:
                await _push_stremio_connection(
                    db,
                    conn,
                    user_id,
                    api_key=api_key,
                    changed_media_ids=stremio_eligible_ids,
                    watch_overrides=watch_overrides,
                )
            except Exception:
                logger.exception(
                    "Failed to push watch state to Stremio connection %s",
                    conn.id,
                )
        await db.commit()


def format_event(event: WatchEvent | PlaybackProgress, media: Media) -> dict:
    # PlaybackProgress has no watched_at; its updated_at remains the display timestamp.
    # A WatchEvent's watched_at may be None (unknown watch date) — preserve that as-is.
    watched_at = event.watched_at if isinstance(event, WatchEvent) else event.updated_at

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
            "tvdb_sourced": is_unmapped_tvdb_episode(media),
        },
        "user_id": event.user_id,
        "watched_at": watched_at.isoformat() if watched_at else None,
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
    current_user: User = Depends(get_current_user_or_api_key),
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
        .order_by(WatchEvent.watched_at.desc().nulls_last(), WatchEvent.id.desc())
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
    current_user: User = Depends(get_current_user_or_api_key),
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
                "tvdb_sourced": is_unmapped_tvdb_episode(media),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
        "tvdb_sourced": is_unmapped_tvdb_episode(media),
    }
    if media.media_type == MediaType.episode and media.show:
        data["show_title"] = media.show.title
        data["show_poster_path"] = media.show.poster_path
        data["show_backdrop_path"] = media.show.backdrop_path
        data["show_tmdb_id"] = media.show.tmdb_id
        data["show_tvdb_id"] = media.show.tvdb_id
    return data


def _compute_next_episode(seasons: list[dict], season: int, episode: int) -> tuple[int, int] | None:
    """Given a show's TMDB season metadata and the last-watched (season, episode),
    returns the next (season, episode), or None if the show has no more aired/
    known episodes after it. Specials (season 0) are never returned."""
    real_seasons = sorted(
        (s for s in seasons if s.get("season_number", 0) > 0),
        key=lambda s: s["season_number"],
    )
    current_season = next((s for s in real_seasons if s["season_number"] == season), None)
    if current_season and episode < current_season.get("episode_count", 0):
        return season, episode + 1
    upcoming = next(
        (s for s in real_seasons if s["season_number"] > season and s.get("episode_count", 0) > 0),
        None,
    )
    if upcoming:
        return upcoming["season_number"], 1
    return None


def _group_last_watched(
    rows: list[tuple[int, int, int, datetime | None]],
) -> tuple[dict[int, tuple[int, int]], dict[int, datetime]]:
    """Reduce next-up candidate rows (ordered by show, season desc, episode desc)
    to each show's furthest-watched (season, episode) and its most recent
    watched_at. watched_at may be NULL (e.g. imported history with no date), so
    a show with no timestamped watch simply gets no last_watched_at entry."""
    last_per_show: dict[int, tuple[int, int]] = {}
    last_watched_at: dict[int, datetime] = {}
    for show_id, season, episode, watched_at in rows:
        if season is None or episode is None:
            # Faulty history entry with an unknown season/episode (e.g. a
            # pre-fix scrobble that lost season 0) - skip it rather than let
            # it corrupt this show's position or crash the fallback lookup
            # below, which assumes season/episode are always ints.
            continue
        if show_id not in last_per_show:
            last_per_show[show_id] = (season, episode)
        if watched_at and (show_id not in last_watched_at or watched_at > last_watched_at[show_id]):
            last_watched_at[show_id] = watched_at
    return last_per_show, last_watched_at


def _has_aired(release_date: str | None, today: date) -> bool:
    """True if release_date (ISO 8601, e.g. from TMDB air_date) is on or before
    today, or unknown. ISO 8601 strings sort lexicographically the same as their
    dates, so a plain string comparison is safe here.

    Used for episodes already confirmed to exist (they're in a season's own
    episode list, e.g. the bulk mark-season/show-watched loops) - there, an
    unknown date is a metadata gap, not a sign the episode isn't real yet, so
    it's treated as aired rather than hidden. See _has_confirmed_air_date for
    the opposite case."""
    return not release_date or release_date <= today.isoformat()


def _has_confirmed_air_date(release_date: str | None, today: date) -> bool:
    """True only if release_date is set AND on or before today.

    Used for Next Up specifically (issue #111): the suggested "next" episode
    is often a placeholder TMDB has pre-created for a renewed show before an
    air date is announced. Unlike _has_aired's callers, there's no other
    confirmation this episode actually exists yet, so an unknown date must
    NOT be treated as "aired" here - that would suggest watching something
    that may not even be out."""
    return bool(release_date) and release_date <= today.isoformat()


class _NextUpEpisodeNotOnTmdb(Exception):
    """Internal signal to roll back a speculative next-up episode row when TMDB
    doesn't actually have it — not a real error, never raised past get_next_up."""


def _remaining_episode_stats(
    season_ep_counts: dict[int, int],
    watched_per_season: dict[int, int],
    avg_runtime: float | None,
) -> dict | None:
    """episodes_left / remaining_runtime for one show's Next Up card (#170).

    season_ep_counts is capped_season_episode_counts output (released episodes
    per season); watched counts are capped per season so provider numbering
    mismatches (more local watched rows than TMDB says a season has) can't push
    the remainder negative. Specials (season 0) are excluded, matching
    total_aired_episodes. The caller only asks about shows that have an aired
    unwatched episode, so the count is clamped to at least 1 even when stale
    TMDB season data hasn't caught up with the episode that just aired.
    """
    total_aired = sum(v for sn, v in season_ep_counts.items() if sn != 0)
    if not total_aired:
        return None
    watched_aired = sum(
        min(watched_per_season.get(sn, 0), cnt)
        for sn, cnt in season_ep_counts.items()
        if sn != 0
    )
    episodes_left = max(total_aired - watched_aired, 1)
    remaining_runtime = int(round(episodes_left * float(avg_runtime))) if avg_runtime else None
    return {"episodes_left": episodes_left, "remaining_runtime": remaining_runtime}


async def _next_up_remaining_stats(
    db: AsyncSession,
    user_id: int,
    next_up_media: list[Media],
    active_rewatch_by_show: dict[int, ShowRewatch],
) -> dict[int, dict]:
    """Batched per-show remaining-episode estimates for the Next Up items (#170).

    Watched counts follow the show detail page's definition (any WatchEvent for
    the episode, or RewatchProgress on the active rewatch) so the numbers agree
    with the watch percentages shown there. Runtime is estimated from the
    average effective runtime of the show's local episode rows, falling back to
    TMDB's episode_run_time; None when neither is known.
    """
    shows_by_id = {m.show_id: m.show for m in next_up_media if m.show_id and m.show}
    if not shows_by_id:
        return {}

    watched_per: dict[tuple[int, int], int] = {}
    non_rewatch_ids = [sid for sid in shows_by_id if sid not in active_rewatch_by_show]
    if non_rewatch_ids:
        watch_a = aliased(WatchEvent)
        rows = await db.execute(
            select(
                Media.show_id,
                Media.season_number,
                func.count(func.distinct(
                    case((watch_a.id.isnot(None), Media.episode_number), else_=None)
                )),
            )
            .outerjoin(watch_a, and_(watch_a.media_id == Media.id, watch_a.user_id == user_id))
            .where(
                Media.show_id.in_(non_rewatch_ids),
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.episode_number.isnot(None),
            )
            .group_by(Media.show_id, Media.season_number)
        )
        for sid, sn, cnt in rows.all():
            watched_per[(sid, sn)] = cnt

    rewatch_show_ids = [sid for sid in shows_by_id if sid in active_rewatch_by_show]
    if rewatch_show_ids:
        rewatch_ids = [active_rewatch_by_show[sid].id for sid in rewatch_show_ids]
        rows = await db.execute(
            select(Media.show_id, Media.season_number, func.count(func.distinct(Media.episode_number)))
            .select_from(RewatchProgress)
            .join(Media, Media.id == RewatchProgress.media_id)
            .where(
                RewatchProgress.rewatch_id.in_(rewatch_ids),
                Media.show_id.in_(rewatch_show_ids),
                Media.season_number.isnot(None),
                Media.episode_number.isnot(None),
            )
            .group_by(Media.show_id, Media.season_number)
        )
        for sid, sn, cnt in rows.all():
            watched_per[(sid, sn)] = cnt

    # Average effective runtime per show — same coalesce as profile.py's watch
    # time stats: episodes often only carry runtime in tmdb_data['runtime'].
    from sqlalchemy import Integer as SAInteger
    from sqlalchemy.types import Text as SAText

    json_runtime = func.cast(
        func.nullif(func.cast(Media.tmdb_data["runtime"], SAText), "null"),
        SAInteger,
    )
    avg_rows = await db.execute(
        select(Media.show_id, func.avg(func.coalesce(Media.runtime, json_runtime)))
        .where(
            Media.show_id.in_(list(shows_by_id)),
            Media.media_type == MediaType.episode,
        )
        .group_by(Media.show_id)
    )
    avg_by_show = {sid: float(avg) for sid, avg in avg_rows.all() if avg is not None}

    stats: dict[int, dict] = {}
    for sid, show in shows_by_id.items():
        season_counts = capped_season_episode_counts(show)
        avg_runtime = avg_by_show.get(sid)
        if not avg_runtime:
            run_times = (show.tmdb_data or {}).get("episode_run_time") or []
            avg_runtime = run_times[0] if run_times else None
        per_season = {sn: cnt for (s, sn), cnt in watched_per.items() if s == sid}
        show_stats = _remaining_episode_stats(season_counts, per_season, avg_runtime)
        if show_stats:
            stats[sid] = show_stats
    return stats


@router.get("/next-up")
async def get_next_up(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
    limit: int | None = None,
    include_hidden: bool = Query(False),
):
    """Next unwatched episode for each show the user is actively watching."""
    # Shows the user is mid-rewatch on read their Next Up position from that
    # rewatch's progress instead of full history - excluded from the Step 1
    # history query below and handled separately in Step 1b, so a show
    # already fully watched (the common rewatch case) still surfaces here.
    active_rewatches_result = await db.execute(
        select(ShowRewatch).where(ShowRewatch.user_id == current_user.id)
    )
    active_rewatches = active_rewatches_result.scalars().all()
    active_rewatch_show_ids = {r.show_id for r in active_rewatches}
    active_rewatch_by_show = {r.show_id: r for r in active_rewatches}

    # Step 1: Find the last watched / significantly-viewed episode per show,
    # and the most recent watch timestamp per show for final sorting.
    watch_filters = [
        WatchEvent.user_id == current_user.id,
        Media.media_type == MediaType.episode,
        Media.show_id.isnot(None),
        or_(WatchEvent.completed == True, WatchEvent.progress_percent >= 0.5),
    ]
    if active_rewatch_show_ids:
        watch_filters.append(Media.show_id.notin_(active_rewatch_show_ids))
    result = await db.execute(
        select(Media.show_id, Media.season_number, Media.episode_number, WatchEvent.watched_at)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(*watch_filters)
        .order_by(Media.show_id, desc(Media.season_number), desc(Media.episode_number))
    )
    rows = result.all()

    # Keep only the furthest episode per show, and the most recent watched_at per show.
    last_per_show, last_watched_at = _group_last_watched(rows)

    # Step 1b: Rewatching shows - candidate position comes from that rewatch's
    # progress (furthest re-watched episode so far), defaulting to "before
    # S1E1" so a freshly-started rewatch with no progress yet still surfaces
    # the first episode instead of being skipped like a never-watched show.
    if active_rewatch_show_ids:
        progress_result = await db.execute(
            select(ShowRewatch.show_id, Media.season_number, Media.episode_number, WatchEvent.watched_at)
            .select_from(RewatchProgress)
            .join(ShowRewatch, ShowRewatch.id == RewatchProgress.rewatch_id)
            .join(Media, Media.id == RewatchProgress.media_id)
            .join(WatchEvent, WatchEvent.id == RewatchProgress.watch_event_id)
            .where(ShowRewatch.user_id == current_user.id)
            .order_by(ShowRewatch.show_id, desc(Media.season_number), desc(Media.episode_number))
        )
        progress_last_per_show, progress_last_watched_at = _group_last_watched(progress_result.all())
        for show_id in active_rewatch_show_ids:
            last_per_show[show_id] = progress_last_per_show.get(show_id, (1, 0))
            last_watched_at[show_id] = progress_last_watched_at.get(
                show_id, active_rewatch_by_show[show_id].started_at
            )

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
        .where(
            Media.media_type == MediaType.episode,
            # Exclude phantom placeholder rows (imported watch/rating history for
            # an episode number TMDB doesn't actually have, e.g. a provider
            # numbering mismatch) — they have no real metadata and would surface
            # a broken Next Up card that 404s when opened.
            Media.tmdb_id.isnot(None),
            or_(*show_filters),
        )
        .order_by(Media.show_id, Media.season_number, Media.episode_number)
    )
    candidates = candidates_result.scalars().all()

    # Take only the immediately next episode per show
    next_per_show: dict[int, Media] = {}
    for media in candidates:
        if media.show_id not in next_per_show:
            next_per_show[media.show_id] = media

    # Fallback for shows with no local row for the next episode yet — e.g. Kodi,
    # which has no library sync, only ever creates a Media row for an episode
    # once it's actually played. Compute the next episode from the show's TMDB
    # season metadata and create/enrich it on demand instead of requiring it to
    # already exist locally.
    missing_show_ids = set(last_per_show) - set(next_per_show)
    if missing_show_ids:
        api_key = await get_user_tmdb_key(db, current_user.id)
        if check_tmdb_key(api_key):
            shows_result = await db.execute(select(Show).where(Show.id.in_(missing_show_ids)))
            shows_by_id = {s.id: s for s in shows_result.scalars().all()}
            for show_id in missing_show_ids:
                show = shows_by_id.get(show_id)
                if not show or not show.tmdb_id:
                    continue
                season, episode = last_per_show[show_id]
                next_ep = _compute_next_episode((show.tmdb_data or {}).get("seasons", []), season, episode)
                if next_ep is None:
                    continue
                next_season_num, next_episode_num = next_ep

                media = Media(
                    media_type=MediaType.episode,
                    title="",  # title is NOT NULL; enrich_media below fills in the
                    # real one — it now runs after the flush, not before, so this
                    # placeholder is needed for that first INSERT to succeed.
                    show_id=show.id,
                    season_number=next_season_num,
                    episode_number=next_episode_num,
                )
                # Flush inside a savepoint before enriching, not after — gives the
                # row a real id for enrich_media's own failure logging, without
                # permanently persisting a phantom episode if the TMDB lookup
                # below turns out to fail (e.g. unreleased episode, or a
                # provider numbering mismatch): the savepoint rolls the insert
                # back on that path instead of ever committing it.
                #
                # The episode's real tmdb_id isn't known until enrich_media
                # resolves it, so it can't go through create_media_safely up
                # front — flushed explicitly here instead, inside the same
                # savepoint, so a conflict with a concurrently-created row for
                # this exact episode is caught right here instead of surfacing
                # at the batched db.commit() below and crashing the endpoint.
                try:
                    async with db.begin_nested():
                        db.add(media)
                        await db.flush()
                        from routers.webhooks import _resolve_tvdb_fallback

                        tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, current_user.id)
                        await enrich_media(
                            media, api_key=api_key, series_tmdb_id=show.tmdb_id,
                            tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
                        )
                        if not media.tmdb_id:
                            raise _NextUpEpisodeNotOnTmdb()
                        resolved_tmdb_id = media.tmdb_id
                        await db.flush()
                except _NextUpEpisodeNotOnTmdb:
                    continue
                except IntegrityError:
                    existing_result = await db.execute(
                        select(Media)
                        .where(Media.tmdb_id == resolved_tmdb_id, Media.media_type == MediaType.episode)
                        .order_by(Media.id)
                    )
                    media = existing_result.scalars().first()
                    if not media:
                        continue
                media.show = show
                next_per_show[show_id] = media
            await db.commit()

    if not next_per_show:
        return {"next_up": []}

    # Remove episodes the user has already (re)watched. For a rewatching show
    # this must check that rewatch's own progress, not full history - the
    # candidate episode is very likely already in history from before the
    # rewatch started, which shouldn't hide it again.
    non_rewatch_ids = [m.id for m in next_per_show.values() if m.show_id not in active_rewatch_show_ids]
    rewatch_ids = [m.id for m in next_per_show.values() if m.show_id in active_rewatch_show_ids]

    completed_ids: set[int] = set()
    if non_rewatch_ids:
        completed_result = await db.execute(
            select(WatchEvent.media_id)
            .where(
                WatchEvent.user_id == current_user.id,
                WatchEvent.completed == True,
                WatchEvent.media_id.in_(non_rewatch_ids),
            )
        )
        completed_ids = {row[0] for row in completed_result.all()}
    if rewatch_ids:
        rewatch_completed_result = await db.execute(
            select(RewatchProgress.media_id)
            .join(ShowRewatch, ShowRewatch.id == RewatchProgress.rewatch_id)
            .where(
                ShowRewatch.user_id == current_user.id,
                RewatchProgress.media_id.in_(rewatch_ids),
            )
        )
        completed_ids |= {row[0] for row in rewatch_completed_result.all()}

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    hidden_set = set(settings.next_up_hidden_shows or []) if settings else set()

    today = date.today()
    next_up = [
        m for m in next_per_show.values()
        if m.id not in completed_ids
        and (include_hidden or m.show_id not in hidden_set)
        # Don't surface an episode that hasn't aired yet — the immediately-next
        # episode for the show, not a later one, so we simply show nothing for
        # this show until it airs rather than skipping ahead. An episode with
        # no air date at all (a renewal placeholder TMDB hasn't dated yet) is
        # treated the same as "not aired" here, not "assume it's fine" (#111).
        and _has_confirmed_air_date(m.release_date, today)
    ]
    next_up.sort(key=lambda m: last_watched_at.get(m.show_id) or datetime.min, reverse=True)
    if limit is not None:
        next_up = next_up[:limit]

    remaining_stats = await _next_up_remaining_stats(
        db, current_user.id, next_up, active_rewatch_by_show
    )

    items = [_format_media_item(m) for m in next_up]
    for item in items:
        item["next_up_hidden"] = item.get("show_id") in hidden_set
        show_stats = remaining_stats.get(item.get("show_id"))
        if show_stats:
            item["episodes_left"] = show_stats["episodes_left"]
            item["remaining_runtime"] = show_stats["remaining_runtime"]
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
from core.enrichment import enrich_media, enrich_episode_from_tvdb, tmdb_season_covers, is_unmapped_tvdb_episode, enrich_media_safely
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
    series_tvdb_id: int | None = None  # links the show to TVDB on demand, see #101
    season_number: int
    episode_order: str | None = None
    watched_at: datetime | None = None  # omitted = now; explicit null = unknown date


class ShowWatchRequest(BaseModel):
    series_tmdb_id: int
    series_tvdb_id: int | None = None  # links the show to TVDB on demand, see #101
    watched_at: datetime | None = None  # omitted = now; explicit null = unknown date


@router.post("", response_model=dict)
async def mark_as_watched(
    event_in: schemas.WatchEventCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. Check if Media exists locally
    media = None
    show = None
    api_key = None
    episode_has_context = (
        event_in.media_type == MediaType.episode
        and event_in.series_tmdb_id is not None
        and event_in.season_number is not None
        and event_in.episode_number is not None
    )

    if episode_has_context:
        from routers.media import get_user_tmdb_key
        from routers.webhooks import _find_or_create_show

        api_key = await get_user_tmdb_key(db, current_user.id)
        try:
            show = await _find_or_create_show(db, event_in.series_tmdb_id, api_key)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"TMDB Media not found: {e}")

        # Link this show to TVDB right away if the client already knows the id
        # (e.g. a Next Up/list card carrying show_tvdb_id) — don't require the
        # user to have visited the show's TVDB page first for the fallback
        # below to be reachable (see #101). Never overwrite an existing
        # different link (tvdb_id is unique).
        if event_in.series_tvdb_id and not show.tvdb_id:
            show.tvdb_id = event_in.series_tvdb_id
            await db.flush()
            await db.commit()

        # Prefer looking up episodes by their show context since tmdb_id may not
        # be set on Media records created via TVDB or webhook paths.
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
        media = result.scalars().first()

    # A previous manual mark may have created the episode before its parent show
    # existed locally. Adopt and re-enrich that orphan now that the UI supplied
    # the complete episode context.
    if media and show and not media.show_id:
        media.show_id = show.id
        media.season_number = event_in.season_number
        media.episode_number = event_in.episode_number
        if not media.poster_path or media.tmdb_data is None:
            from routers.webhooks import _resolve_tvdb_fallback

            tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, current_user.id)
            media = await enrich_media_safely(
                db, media, api_key=api_key, series_tmdb_id=event_in.series_tmdb_id,
                tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
            )

    # 2. If not, create Media record from TMDB
    if not media:
        if api_key is None:
            from routers.media import get_user_tmdb_key

            api_key = await get_user_tmdb_key(db, current_user.id)

        try:
            if event_in.media_type == MediaType.movie:
                data = await tmdb.get_movie(event_in.tmdb_id, api_key=api_key)
                media, _created = await create_media_safely(
                    db, event_in.tmdb_id, event_in.media_type, title=data.get("title")
                )
                await enrich_media(media, api_key=api_key)
            elif episode_has_context:
                ep_data = None
                try:
                    ep_data = await tmdb.get_episode(
                        event_in.series_tmdb_id, event_in.season_number, event_in.episode_number, api_key=api_key
                    )
                except Exception:
                    ep_data = None

                if ep_data:
                    media, _created = await create_media_safely(
                        db,
                        ep_data.get("id"),
                        MediaType.episode,
                        title=ep_data.get("name"),
                        season_number=event_in.season_number,
                        episode_number=event_in.episode_number,
                        show_id=show.id,
                    )
                    await enrich_media(media, api_key=api_key, series_tmdb_id=event_in.series_tmdb_id)
                elif show.tvdb_id:
                    # Not on TMDB (e.g. TMDB is sparse for this show, see #101)
                    # — fall back to TVDB, which this show is also linked to.
                    from routers.shows import get_user_tvdb_key
                    import core.tvdb as tvdb_client

                    tvdb_api_key = await get_user_tvdb_key(db, current_user.id)
                    if not tvdb_api_key:
                        raise HTTPException(status_code=404, detail="Episode not found on TMDB, and no TVDB key configured to check TVDB")
                    tvdb_lang = tvdb_client.tvdb_language(await get_user_metadata_language(db, current_user.id))
                    try:
                        raw_eps = await tvdb_client.get_series_episodes(show.tvdb_id, event_in.season_number, tvdb_api_key, language=tvdb_lang)
                    except Exception as e:
                        raise HTTPException(status_code=404, detail=f"Episode not found on TMDB or TVDB: {e}")
                    tvdb_ep = next(
                        (tvdb_client.format_episode(e) for e in raw_eps if e.get("number") == event_in.episode_number),
                        None,
                    )
                    if not tvdb_ep:
                        raise HTTPException(status_code=404, detail="Episode not found on TMDB or TVDB")
                    media = Media(
                        media_type=MediaType.episode,
                        season_number=event_in.season_number,
                        episode_number=event_in.episode_number,
                        show_id=show.id,
                    )
                    # tmdb_id isn't known until enrich_episode_from_tvdb resolves it
                    # (it stores the TVDB episode id there), so this can't go
                    # through create_media_safely up front - flushed explicitly
                    # here instead, inside a savepoint, so a conflict with a
                    # concurrently-created row for this exact episode is caught
                    # right here instead of poisoning the whole transaction.
                    try:
                        async with db.begin_nested():
                            db.add(media)
                            await db.flush()
                            await enrich_episode_from_tvdb(media, tvdb_ep)
                            resolved_tmdb_id = media.tmdb_id
                            await db.flush()
                    except IntegrityError:
                        existing_result = await db.execute(
                            select(Media)
                            .where(Media.tmdb_id == resolved_tmdb_id, Media.media_type == MediaType.episode)
                            .order_by(Media.id)
                        )
                        existing = existing_result.scalars().first()
                        if not existing:
                            raise
                        media = existing
                else:
                    raise HTTPException(status_code=404, detail="Episode not found on TMDB")
            else:
                raise HTTPException(status_code=404, detail="Episode context required (series_tmdb_id, season_number, episode_number)")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"TMDB Media not found: {e}")

    # 3. Create WatchEvent
    # Omitted watched_at retains the existing API default ("now"); explicit
    # null marks the play watched without a known date.
    watched_at = (
        event_in.watched_at.replace(tzinfo=None) if event_in.watched_at is not None
        else None if "watched_at" in event_in.model_fields_set
        else datetime.utcnow()
    )
    event = WatchEvent(
        user_id=current_user.id,
        media_id=media.id,
        watched_at=watched_at,
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

    if event_in.completed:
        await record_rewatch_progress(db, current_user.id, media.id, event.id)
        await db.commit()

    # 4. Push to media servers if outbound push is enabled
    if event_in.completed:
        await _push_watch_state(
            db, current_user.id, [media.id], watched=True,
            watched_at_by_media={media.id: watched_at},
        )

    return {"status": "ok", "message": f"Marked {media.title} as watched"}


@router.get("/item-events")
async def get_item_events(
    tmdb_id: int = Query(...),
    media_type: MediaType = Query(...),
    series_tmdb_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Return all completed watch events for a specific movie or episode,
    plus whether it currently counts as watched. Full play history is always
    returned as-is - but for an episode whose show has an active rewatch,
    "watched" reflects that rewatch's own progress rather than raw history,
    since a pre-rewatch play shouldn't make an episode look watched again."""
    query = (
        select(WatchEvent, Media.id)
        .join(Media, Media.id == WatchEvent.media_id)
        .where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.completed == True,
            Media.tmdb_id == tmdb_id,
            Media.media_type == media_type,
        )
        .order_by(WatchEvent.watched_at.desc().nulls_last(), WatchEvent.id.desc())
    )
    result = await db.execute(query)
    rows = result.all()
    events = [row[0] for row in rows]
    media_id = rows[0][1] if rows else None

    watched = len(events) > 0
    if media_type == MediaType.episode and series_tmdb_id:
        show_q = await db.execute(select(Show).where(Show.tmdb_id == series_tmdb_id))
        show = show_q.scalar_one_or_none()
        if show:
            active_rewatch = await get_active_rewatch(db, current_user.id, show.id)
            if active_rewatch:
                if media_id is None:
                    media_q = await db.execute(
                        select(Media.id)
                        .where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.episode)
                        .order_by(Media.id)
                    )
                    media_id = media_q.scalars().first()
                watched = False
                if media_id is not None:
                    progress_q = await db.execute(
                        select(RewatchProgress.id).where(
                            RewatchProgress.rewatch_id == active_rewatch.id,
                            RewatchProgress.media_id == media_id,
                        )
                    )
                    watched = progress_q.scalar_one_or_none() is not None

    return {
        "watched": watched,
        "events": [
            {"id": e.id, "watched_at": e.watched_at.isoformat() if e.watched_at else None}
            for e in events
        ],
    }


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
    # Any active rewatch is meaningless once its underlying history is gone -
    # RewatchProgress cascades from WatchEvent deletion already, but the
    # ShowRewatch "currently rewatching" marker itself doesn't depend on any
    # WatchEvent and would otherwise survive a full clear, stuck at 0
    # progress forever with nothing left to progress it.
    await db.execute(delete(ShowRewatch).where(ShowRewatch.user_id == current_user.id))
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
            select(Media)
            .where(Media.tmdb_id == tmdb_id, Media.media_type == media_type)
            .order_by(Media.id)
        )
        media = media_q.scalars().first()
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

    if body.series_tvdb_id and not show.tvdb_id:
        show.tvdb_id = body.series_tvdb_id
        await db.flush()
        await db.commit()

    target_positions: set[tuple[int, int]] | None = None
    canonical_seasons = [body.season_number]
    tvdb_fallback_episodes: list[dict] | None = None
    if body.episode_order == "tvdb":
        mapping_result = await db.execute(
            select(EpisodeOrderMapping).where(
                EpisodeOrderMapping.series_tmdb_id == body.series_tmdb_id,
                EpisodeOrderMapping.tvdb_season_number == body.season_number,
            )
        )
        mappings = list(mapping_result.scalars().all())
        if not mappings:
            # No computed mapping — if TMDB doesn't even have a season with
            # this number, it's confidently absent (see #101): fetch straight
            # from TVDB instead of guessing or 400ing. If TMDB DOES have a
            # season here, stay conservative — don't guess positions.
            season_on_tmdb = any(
                s.get("season_number") == body.season_number
                for s in (show.tmdb_data or {}).get("seasons", [])
            )
            if season_on_tmdb or not show.tvdb_id:
                raise HTTPException(status_code=400, detail="TVDB episode mapping is not available")
            from routers.shows import get_user_tvdb_key
            import core.tvdb as tvdb_client

            tvdb_api_key = await get_user_tvdb_key(db, current_user.id)
            if not tvdb_api_key:
                raise HTTPException(status_code=400, detail="TVDB API key not configured")
            tvdb_lang = tvdb_client.tvdb_language(await get_user_metadata_language(db, current_user.id))
            try:
                raw_eps = await tvdb_client.get_series_episodes(show.tvdb_id, body.season_number, tvdb_api_key, language=tvdb_lang)
            except Exception as e:
                raise HTTPException(status_code=404, detail=f"TVDB season fetch failed: {e}")
            tvdb_fallback_episodes = [tvdb_client.format_episode(e) for e in raw_eps]
        else:
            target_positions = {
                (mapping.tmdb_season_number, mapping.tmdb_episode_number)
                for mapping in mappings
            }
            canonical_seasons = sorted({season for season, _ in target_positions})

    now = datetime.utcnow()
    today = now.date()
    # "Has this episode aired yet" stays tied to the real current date, independent
    # of what date the user says they watched it. Omitted watched_at retains the
    # existing API default ("now"); explicit null marks it watched without a known date.
    resolved_watched_at = (
        body.watched_at.replace(tzinfo=None) if body.watched_at is not None
        else None if "watched_at" in body.model_fields_set
        else now
    )

    all_season_episodes = []
    if tvdb_fallback_episodes is not None:
        existing_q = await db.execute(
            select(Media).where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number == body.season_number,
            )
        )
        existing_map = {
            (media.season_number, media.episode_number): media
            for media in existing_q.scalars().all()
        }
        for tvdb_ep in tvdb_fallback_episodes:
            if tvdb_ep.get("episode_number") is None or not _has_aired(tvdb_ep.get("air_date"), today):
                continue
            position = (body.season_number, tvdb_ep["episode_number"])
            existing = existing_map.get(position)
            if existing:
                all_season_episodes.append(existing)
                continue
            new_ep = Media(
                show_id=show.id,
                media_type=MediaType.episode,
                season_number=body.season_number,
                episode_number=tvdb_ep["episode_number"],
            )
            # tmdb_id isn't known until enrich_episode_from_tvdb resolves it, so
            # this can't go through create_media_safely up front - flushed
            # explicitly here instead, inside a savepoint, so a conflict with a
            # concurrently-created row for this exact episode is caught right
            # here instead of failing the whole batch's flush later.
            await enrich_episode_from_tvdb(new_ep, tvdb_ep)
            try:
                async with db.begin_nested():
                    db.add(new_ep)
                    await db.flush()
            except IntegrityError:
                existing_result = await db.execute(
                    select(Media)
                    .where(Media.tmdb_id == new_ep.tmdb_id, Media.media_type == MediaType.episode)
                    .order_by(Media.id)
                )
                existing = existing_result.scalars().first()
                if not existing:
                    raise
                new_ep = existing
            all_season_episodes.append(new_ep)
    else:
        try:
            season_payloads = await asyncio.gather(
                *(
                    tmdb.get_season(
                        body.series_tmdb_id,
                        canonical_season,
                        api_key=api_key,
                    )
                    for canonical_season in canonical_seasons
                )
            )
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Season not found: {e}")

        existing_q = await db.execute(
            select(Media).where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number.in_(canonical_seasons),
            )
        )
        existing_map = {
            (media.season_number, media.episode_number): media
            for media in existing_q.scalars().all()
        }

        for canonical_season, season_data in zip(canonical_seasons, season_payloads):
            for ep in season_data.get("episodes", []):
                position = (canonical_season, ep["episode_number"])
                if target_positions is not None and position not in target_positions:
                    continue
                air_date_str = ep.get("air_date")
                if not air_date_str:
                    continue
                try:
                    air_date = datetime.strptime(air_date_str, "%Y-%m-%d").date()
                    if air_date > today:
                        continue
                except Exception:
                    continue

                existing = existing_map.get(position)
                if existing:
                    all_season_episodes.append(existing)
                    continue
                new_ep, _created = await create_media_safely(
                    db,
                    ep["id"],
                    MediaType.episode,
                    show_id=show.id,
                    title=ep.get("name") or f"Episode {ep['episode_number']}",
                    season_number=canonical_season,
                    episode_number=ep["episode_number"],
                    poster_path=tmdb.poster_url(ep.get("still_path"), size="w500"),
                    release_date=air_date_str,
                    tmdb_rating=ep.get("vote_average"),
                )
                all_season_episodes.append(new_ep)

    await db.flush() # Get IDs for new episodes
    
    # 4. Mark all as watched
    if not all_season_episodes:
        return {"status": "ok", "count": 0}

    already_watched = await get_already_watched_for_bulk_mark(
        db, current_user.id, show, [ep.id for ep in all_season_episodes]
    )

    newly_watched = []
    new_events = []
    for ep in all_season_episodes:
        if ep.id not in already_watched:
            event = WatchEvent(
                user_id=current_user.id,
                media_id=ep.id,
                watched_at=resolved_watched_at,
                completed=True,
                play_count=1,
                progress_percent=1.0,
            )
            db.add(event)
            new_events.append(event)
            newly_watched.append(ep.id)

    if newly_watched:
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == current_user.id,
                PlaybackProgress.media_id.in_(newly_watched),
            )
        )
    await db.commit()
    if new_events:
        for event in new_events:
            await record_rewatch_progress(db, current_user.id, event.media_id, event.id)
        await db.commit()
    await _push_watch_state(db, current_user.id, newly_watched, watched=True)
    return {"status": "ok", "count": len(newly_watched)}


@router.delete("/season")
async def unwatch_season(
    series_tmdb_id: int = Query(...),
    season_number: int = Query(...),
    episode_order: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove all watch events for a season."""
    show_q = await db.execute(select(Show).where(Show.tmdb_id == series_tmdb_id))
    show = show_q.scalar_one_or_none()
    if not show:
        return {"status": "ok", "count": 0}

    media_filters = [
        Media.show_id == show.id,
        Media.media_type == MediaType.episode,
    ]
    if episode_order == "tvdb":
        mapping_result = await db.execute(
            select(EpisodeOrderMapping).where(
                EpisodeOrderMapping.series_tmdb_id == series_tmdb_id,
                EpisodeOrderMapping.tvdb_season_number == season_number,
            )
        )
        positions = [
            and_(
                Media.season_number == mapping.tmdb_season_number,
                Media.episode_number == mapping.tmdb_episode_number,
            )
            for mapping in mapping_result.scalars().all()
        ]
        if not positions:
            # No computed mapping. If TMDB doesn't have a season with this
            # number at all, these episodes were tracked via the raw TVDB
            # numbers (see #101) — fall back to that. Otherwise stay
            # conservative and no-op rather than guess positions.
            season_on_tmdb = any(
                s.get("season_number") == season_number
                for s in (show.tmdb_data or {}).get("seasons", [])
            )
            if season_on_tmdb:
                return {"status": "ok", "count": 0}
            media_filters.append(Media.season_number == season_number)
        else:
            media_filters.append(or_(*positions))
    else:
        media_filters.append(Media.season_number == season_number)

    episodes_q = await db.execute(select(Media.id).where(*media_filters))
    episode_ids = [row[0] for row in episodes_q.all()]
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

    if body.series_tvdb_id and not show.tvdb_id:
        show.tvdb_id = body.series_tvdb_id
        await db.flush()
        await db.commit()

    # 2. For each season, fetch episodes and ensure they exist + mark watched
    seasons = [s["season_number"] for s in show.tmdb_data["seasons"] if s["season_number"] > 0]
    all_newly_watched_ids = []
    all_new_events = []

    now = datetime.utcnow()
    today = now.date()
    # See mark_season_watched: aired-cutoff stays tied to the real current date;
    # omitted watched_at retains "now", explicit null means unknown watch date.
    resolved_watched_at = (
        body.watched_at.replace(tzinfo=None) if body.watched_at is not None
        else None if "watched_at" in body.model_fields_set
        else now
    )

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
                new_ep, _created = await create_media_safely(
                    db,
                    ep["id"],
                    MediaType.episode,
                    show_id=show.id,
                    title=ep.get("name") or f"Episode {ep_num}",
                    season_number=sn,
                    episode_number=ep_num,
                    poster_path=tmdb.poster_url(ep.get("still_path"), size="w500"),
                    release_date=air_date_str,
                    tmdb_rating=ep.get("vote_average"),
                )
                season_eps_to_watch.append(new_ep)
        
        await db.flush()
        
        if not season_eps_to_watch: continue

        already_watched = await get_already_watched_for_bulk_mark(
            db, current_user.id, show, [ep.id for ep in season_eps_to_watch]
        )

        for ep in season_eps_to_watch:
            if ep.id not in already_watched:
                event = WatchEvent(
                    user_id=current_user.id,
                    media_id=ep.id,
                    watched_at=resolved_watched_at,
                    completed=True,
                    play_count=1,
                    progress_percent=1.0,
                )
                db.add(event)
                all_new_events.append(event)
                all_newly_watched_ids.append(ep.id)

    # 3. Seasons TVDB has but TMDB doesn't (see #101) — mirrors step 2 above
    # but sourced from TVDB, only reachable if this show is also linked to a
    # TVDB id (set once the user visits its TVDB-numbered page).
    if show.tvdb_id:
        from routers.shows import get_user_tvdb_key
        import core.tvdb as tvdb_client

        tvdb_api_key = await get_user_tvdb_key(db, current_user.id)
        if tvdb_api_key:
            tvdb_lang = tvdb_client.tvdb_language(await get_user_metadata_language(db, current_user.id))
            tmdb_season_numbers = {s["season_number"] for s in show.tmdb_data.get("seasons", [])}
            try:
                tvdb_show_data = tvdb_client.format_series(await tvdb_client.get_series(show.tvdb_id, tvdb_api_key), language=tvdb_lang)
            except Exception:
                tvdb_show_data = None

            if tvdb_show_data:
                tvdb_only_seasons = [
                    s["season_number"] for s in tvdb_show_data.get("seasons", [])
                    if s.get("season_number") and s["season_number"] > 0 and s["season_number"] not in tmdb_season_numbers
                ]
                for sn in tvdb_only_seasons:
                    try:
                        tvdb_eps = [tvdb_client.format_episode(e) for e in await tvdb_client.get_series_episodes(show.tvdb_id, sn, tvdb_api_key, language=tvdb_lang)]
                    except Exception:
                        continue

                    existing_q = await db.execute(
                        select(Media).where(
                            Media.show_id == show.id,
                            Media.media_type == MediaType.episode,
                            Media.season_number == sn,
                        )
                    )
                    existing_map = {m.episode_number: m for m in existing_q.scalars().all()}

                    season_eps_to_watch = []
                    for ep in tvdb_eps:
                        if ep.get("episode_number") is None or not _has_aired(ep.get("air_date"), today):
                            continue
                        ep_num = ep["episode_number"]
                        if ep_num in existing_map:
                            season_eps_to_watch.append(existing_map[ep_num])
                        else:
                            new_ep = Media(
                                show_id=show.id,
                                media_type=MediaType.episode,
                                season_number=sn,
                                episode_number=ep_num,
                            )
                            # tmdb_id isn't known until enrich_episode_from_tvdb
                            # resolves it, so this can't go through
                            # create_media_safely up front - flushed explicitly
                            # here instead, inside a savepoint, so a conflict
                            # with a concurrently-created row for this exact
                            # episode is caught right here instead of failing
                            # the whole batch's flush later.
                            await enrich_episode_from_tvdb(new_ep, ep)
                            try:
                                async with db.begin_nested():
                                    db.add(new_ep)
                                    await db.flush()
                            except IntegrityError:
                                existing_result = await db.execute(
                                    select(Media)
                                    .where(Media.tmdb_id == new_ep.tmdb_id, Media.media_type == MediaType.episode)
                                    .order_by(Media.id)
                                )
                                existing = existing_result.scalars().first()
                                if not existing:
                                    raise
                                new_ep = existing
                            season_eps_to_watch.append(new_ep)

                    await db.flush()
                    if not season_eps_to_watch:
                        continue

                    already_watched = await get_already_watched_for_bulk_mark(
                        db, current_user.id, show, [ep.id for ep in season_eps_to_watch]
                    )

                    for ep in season_eps_to_watch:
                        if ep.id not in already_watched:
                            event = WatchEvent(
                                user_id=current_user.id,
                                media_id=ep.id,
                                watched_at=resolved_watched_at,
                                completed=True,
                                play_count=1,
                                progress_percent=1.0,
                            )
                            db.add(event)
                            all_new_events.append(event)
                            all_newly_watched_ids.append(ep.id)

    if all_newly_watched_ids:
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == current_user.id,
                PlaybackProgress.media_id.in_(all_newly_watched_ids),
            )
        )
    await db.commit()
    if all_new_events:
        for event in all_new_events:
            await record_rewatch_progress(db, current_user.id, event.media_id, event.id)
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

    # Same reasoning as clear_history: an active rewatch for this show is
    # meaningless once all of its history is gone, and would otherwise
    # survive stuck at 0 progress. Unwatching a single season (unwatch_season,
    # above) deliberately doesn't do this - the rewatch may still be
    # legitimately in progress on the show's other seasons.
    await db.execute(delete(ShowRewatch).where(ShowRewatch.user_id == current_user.id, ShowRewatch.show_id == show.id))
    result = await db.execute(
        delete(WatchEvent).where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.media_id.in_(episode_ids),
        )
    )
    await db.commit()
    await _push_watch_state(db, current_user.id, episode_ids, watched=False)
    return {"status": "ok", "count": result.rowcount}


@router.post("/rewatch")
async def start_rewatch(
    series_tmdb_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start (or restart) a rewatch cycle for a show. Watch history is never
    touched - this just makes the show/season/episode pages and Next Up
    read watched status from a fresh, empty progress cycle instead of full
    history until it's completed or cancelled. Calling this again while a
    cycle is already active resets it, discarding that cycle's progress."""
    show_q = await db.execute(select(Show).where(Show.tmdb_id == series_tmdb_id))
    show = show_q.scalar_one_or_none()
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    existing = await get_active_rewatch(db, current_user.id, show.id)
    if existing:
        await db.execute(delete(ShowRewatch).where(ShowRewatch.id == existing.id))

    rewatch = ShowRewatch(user_id=current_user.id, show_id=show.id)
    db.add(rewatch)
    await db.commit()
    await db.refresh(rewatch)
    return {"status": "ok", "started_at": rewatch.started_at.isoformat()}


@router.delete("/rewatch")
async def cancel_rewatch(
    series_tmdb_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel an active rewatch cycle without finishing it. Watch history is
    untouched; the show goes back to reading watched status from full
    history, same as a naturally-completed rewatch."""
    show_q = await db.execute(select(Show).where(Show.tmdb_id == series_tmdb_id))
    show = show_q.scalar_one_or_none()
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    existing = await get_active_rewatch(db, current_user.id, show.id)
    if not existing:
        return {"status": "ok", "cancelled": False}

    await db.execute(delete(ShowRewatch).where(ShowRewatch.id == existing.id))
    await db.commit()
    return {"status": "ok", "cancelled": True}


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
            select(Media)
            .where(Media.tmdb_id == body.tmdb_id, Media.media_type == body.media_type)
            .order_by(Media.id)
        )
        media = result.scalars().first()
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
        media, _created = await create_media_safely(db, body.tmdb_id, body.media_type, title=title)
        try:
            await enrich_media(media, api_key=api_key)
        except Exception:
            pass
    else:
        # Episode: create a minimal row from request data
        show_id = None
        if body.show_tmdb_id:
            show_q = await db.execute(select(Show).where(Show.tmdb_id == body.show_tmdb_id))
            show = show_q.scalar_one_or_none()
            if show:
                show_id = show.id
        media, _created = await create_media_safely(
            db,
            body.tmdb_id,
            body.media_type,
            title=body.title or "Unknown",
            runtime=body.runtime,
            season_number=body.season_number,
            episode_number=body.episode_number,
            show_id=show_id,
        )
        if show_id is not None and media.show_id is None:
            media.show_id = show_id

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
    new_events: list[WatchEvent] = []
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
        event = WatchEvent(
            user_id=session.user_id,
            media_id=session.media_id,
            watched_at=now,
            completed=True,
            play_count=1,
            progress_percent=1.0,
        )
        db.add(event)
        new_events.append(event)
        completed.append((session.user_id, session.media_id))
    if completed:
        await db.commit()
        for event in new_events:
            await record_rewatch_progress(db, event.user_id, event.media_id, event.id)
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

    event = WatchEvent(
        user_id=current_user.id,
        media_id=media_id,
        watched_at=datetime.utcnow(),
        completed=True,
        play_count=1,
        progress_percent=1.0,
    )
    db.add(event)
    await db.commit()

    await record_rewatch_progress(db, current_user.id, media_id, event.id)
    await db.commit()

    await _push_watch_state(db, current_user.id, [media_id], watched=True)
    return {"status": "ok"}
