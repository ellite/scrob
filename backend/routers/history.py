import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, or_, select, desc, func, delete, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload, aliased
from db import get_db, AsyncSessionLocal
from models.media import Media
from models.show import Show
from models.events import WatchEvent
from models.playback_session import PlaybackSession
from models.playback_progress import PlaybackProgress
from models.collection import Collection, CollectionFile
from models.base import MediaType, CollectionSource
from models.users import UserSettings
from models.connections import MediaServerConnection
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder
from models.rewatch import ShowRewatch, RewatchProgress
from models.ratings import Rating
from routers.media import enrich_with_state, get_user_tmdb_key, check_tmdb_key, _attach_episode_order_fields
from core.translations import get_user_metadata_language, get_media_translations, apply_media_translations
from core.rewatch import get_active_rewatch, record_rewatch_progress, get_already_watched_for_bulk_mark, capped_season_episode_counts
from core.enrichment import create_media_safely
from core.episode_order import get_episode_orders_for_series, get_tmdb_to_tvdb_positions

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
    exclude_connection_id: int | None = None,
) -> None:
    """Fan-out watched/unwatched state to all connections with push_watched enabled.

    exclude_connection_id skips one connection - used when this call was itself
    triggered by an inbound webhook from a media server, so that state isn't
    pushed straight back to the same server. Without it, a two-way-sync
    connection (webhook in + push_watched out) can self-trigger forever: the
    push causes another UserData change on that same server, which re-fires
    its own webhook back into Scrob, which pushes again (see #190) - each
    round trip is a real, unbounded loop of outbound HTTP calls and inbound
    webhook deliveries, not just a redundant write.
    """
    if not media_ids:
        return

    conns_result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == user_id,
            MediaServerConnection.push_watched == True,
        )
    )
    connections = [c for c in conns_result.scalars().all() if c.id != exclude_connection_id]

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

    async def _push_plex_watched_and_record(conn: MediaServerConnection, source_id: str, uid: int, mid: int) -> bool:
        ok = await plex_client.mark_watched(conn.url, conn.token, source_id)
        if ok:
            from routers.sync import _record_plex_pending_push
            await _record_plex_pending_push(uid, mid)
        return ok

    # Jellyfin/Emby's UserDataSaved webhook echoes an outbound mark-watched
    # straight back, and a backdated watched_at slips past _write_watch_event's
    # recent-event guard - so each such push is registered here so the echo is
    # recognised as our own, not a second play stamped "now" (GitHub #324).
    # Mirrors the same registration in routers/sync.py's fan-out.
    from routers.webhooks import mark_pushed_watched

    if connections:
        files_result = await db.execute(
            select(CollectionFile, Collection.media_id)
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .where(
                Collection.user_id == user_id,
                Collection.media_id.in_(media_ids),
            )
        )
        coll_files = files_result.all()

        conn_by_type: dict[str, list[MediaServerConnection]] = {}
        for conn in connections:
            conn_by_type.setdefault(conn.type, []).append(conn)

        for coll_file, coll_media_id in coll_files:
            if not coll_file.source_id:
                continue
            source_type = coll_file.source.value if hasattr(coll_file.source, "value") else str(coll_file.source)
            for conn in conn_by_type.get(source_type, []):
                if coll_file.source == CollectionSource.plex:
                    label = f"plex connection {conn.id}"
                    if watched:
                        tasks.append((label, _push_plex_watched_and_record(conn, coll_file.source_id, user_id, coll_media_id)))
                    else:
                        tasks.append((label, plex_client.mark_unwatched(conn.url, conn.token, coll_file.source_id)))
                elif coll_file.source == CollectionSource.jellyfin:
                    label = f"jellyfin connection {conn.id}"
                    if watched:
                        mark_pushed_watched(user_id, coll_media_id)
                        tasks.append((label, jellyfin_client.mark_watched(conn.url, conn.token, conn.server_user_id, coll_file.source_id)))
                    else:
                        tasks.append((label, jellyfin_client.mark_unwatched(conn.url, conn.token, conn.server_user_id, coll_file.source_id)))
                elif coll_file.source == CollectionSource.emby:
                    label = f"emby connection {conn.id}"
                    if watched:
                        mark_pushed_watched(user_id, coll_media_id)
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

    trakt_token: str | None = None
    if push_trakt and settings.trakt_client_id:
        from routers.trakt import TraktTokenError, ensure_valid_trakt_token
        try:
            trakt_token = await ensure_valid_trakt_token(db, settings)
        except TraktTokenError as exc:
            logger.warning("Skipping Trakt history push for user %s: %s", user_id, exc)

    if trakt_token:
        media_res = await db.execute(
            select(Media).where(Media.id.in_(media_ids))
        )
        media_items = media_res.scalars().all()
        for media in media_items:
            if not media.tmdb_id or is_unmapped_tvdb_episode(media):
                continue
            if media.media_type == MediaType.movie:
                if watched:
                    tasks.append((f"trakt add movie {media.tmdb_id}", trakt_client.add_movie_to_history(settings.trakt_client_id, trakt_token, media.tmdb_id, resolved_watched_at.get(media.id))))
                else:
                    tasks.append((f"trakt remove movie {media.tmdb_id}", trakt_client.remove_movie_from_history(settings.trakt_client_id, trakt_token, media.tmdb_id)))
            elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                show_res = await db.execute(select(Show).where(Show.id == media.show_id))
                show = show_res.scalar_one_or_none()
                if show and show.tmdb_id:
                    if watched:
                        tasks.append((f"trakt add episode {show.tmdb_id} S{media.season_number}E{media.episode_number}", trakt_client.add_episode_to_history(settings.trakt_client_id, trakt_token, show.tmdb_id, media.season_number, media.episode_number, resolved_watched_at.get(media.id))))
                    else:
                        tasks.append((f"trakt remove episode {show.tmdb_id} S{media.season_number}E{media.episode_number}", trakt_client.remove_episode_from_history(settings.trakt_client_id, trakt_token, show.tmdb_id, media.season_number, media.episode_number)))

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
                # A warning with a plain reason, not a full traceback dump -
                # these are almost always an expired/revoked credential on
                # the remote service, not a bug here.
                logger.warning("Can't send history event to %s because %s", label, result)

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
                    # See core/nuvio.py's connection_lock docstring - conn may
                    # have been loaded before another request already rotated
                    # this single-use refresh token while this one waited.
                    await db.refresh(conn)
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
            except Exception as e:
                logger.warning("Can't send history event to Nuvio (connection %s) because %s", conn.id, e)
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
            except Exception as e:
                logger.warning("Can't send history event to Stremio (connection %s) because %s", conn.id, e)
        await db.commit()


def _effective_runtime(media: Media) -> int | None:
    """Media.runtime, falling back to the cached tmdb_data.runtime for rows
    enriched before #169 populated the column - a NULL there freezes the
    Now Playing bar's live progress (#383). The migration backfills existing
    rows; this covers anything that still slips through."""
    if media.runtime:
        return media.runtime
    raw = (media.tmdb_data or {}).get("runtime")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


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
            "runtime": _effective_runtime(media),
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
    q: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    offset = (page - 1) * page_size

    # Watch history is movie/episode only. A watched show or season is a
    # derived state (all of its episodes watched), never a watch event of its
    # own - so a series-level row is always junk from an older buggy import
    # path. Constrain every history query to the two real types so those rows
    # can't surface (they previously leaked into the unfiltered "All" tab and
    # then 404'd trying to open as a movie). See #358.
    type_filter = [type] if type in ("movie", "episode") else ["movie", "episode"]

    # A show's title lives on Show, not on its episode Media rows (those only
    # carry the episode's own title) - so a search for the show name has to
    # reach through the same outer join enrich_with_state/format_event already
    # rely on downstream, not just Media.title.
    search_term = q.strip() if q else None

    base_query = (
        select(func.count())
        .select_from(WatchEvent)
        .join(Media, Media.id == WatchEvent.media_id)
        .outerjoin(Show, Show.id == Media.show_id)
        .where(WatchEvent.user_id == current_user.id)
        .where(WatchEvent.completed == True)
        .where(Media.media_type.in_(type_filter))
    )
    if search_term:
        base_query = base_query.where(
            or_(Media.title.ilike(f"%{search_term}%"), Show.title.ilike(f"%{search_term}%"))
        )

    total_result = await db.execute(base_query)
    total_count = total_result.scalar_one()
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    query = (
        select(WatchEvent, Media)
        .join(Media, Media.id == WatchEvent.media_id)
        .outerjoin(Show, Show.id == Media.show_id)
        .options(selectinload(WatchEvent.media).selectinload(Media.show))
        .where(WatchEvent.user_id == current_user.id)
        .where(WatchEvent.completed == True)
        .where(Media.media_type.in_(type_filter))
        .order_by(WatchEvent.watched_at.desc().nulls_last(), WatchEvent.id.desc())
    )
    if search_term:
        query = query.where(
            or_(Media.title.ilike(f"%{search_term}%"), Show.title.ilike(f"%{search_term}%"))
        )

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
                "runtime": _effective_runtime(media),
                "tvdb_sourced": is_unmapped_tvdb_episode(media),
                "has_mid_credits_scene": (media.tmdb_data or {}).get("has_mid_credits_scene", False),
                "has_post_credits_scene": (media.tmdb_data or {}).get("has_post_credits_scene", False),
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

    # Episode-order preference / TVDB position translation (#186) - this
    # endpoint builds its own dict inline rather than going through
    # enrich_with_state, so it needs the same batched lookups + attach step
    # done separately, in a second pass now that every session's show_tmdb_id
    # is known. Session counts here are always small (active playback only),
    # so a second pass over `sessions` costs nothing.
    series_ids = {
        s["media"]["show_tmdb_id"] for s in sessions if s["media"].get("show_tmdb_id")
    }
    if series_ids:
        episode_orders = await get_episode_orders_for_series(db, current_user.id, list(series_ids))
        tvdb_series_ids = [
            sid for sid, pref in episode_orders.items() if pref.episode_order == "tvdb"
        ]
        tmdb_to_tvdb = (
            await get_tmdb_to_tvdb_positions(db, tvdb_series_ids) if tvdb_series_ids else {}
        )
        for s in sessions:
            _attach_episode_order_fields(s["media"], episode_orders, tmdb_to_tvdb)

    return {"now_playing": sessions}


@router.delete("/sessions")
async def clear_now_playing_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Delete all active playback sessions for the current user."""
    await db.execute(
        delete(PlaybackSession).where(PlaybackSession.user_id == current_user.id)
    )
    await db.commit()
    return {"status": "ok"}


# How recently a completed WatchEvent must have landed for the "rate it now"
# popup to still be worth showing when a session drops off the Now Playing bar.
RATE_PROMPT_WINDOW = timedelta(minutes=15)


@router.get("/rate-prompt")
async def rate_prompt(
    media_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Should the homepage show a "rate this" popup for a just-finished item? (#177)

    Called by the Now Playing poller when a session disappears from the bar.
    Returns should_prompt=True only when the user opted in for this media type,
    a completed WatchEvent for it landed within RATE_PROMPT_WINDOW, and they
    have not already rated it. The media block feeds the popup's poster/title.
    """
    none_response = {"should_prompt": False, "media": None}

    media = (await db.execute(
        select(Media).options(selectinload(Media.show)).where(Media.id == media_id)
    )).scalar_one_or_none()
    if media is None or not media.tmdb_id or is_unmapped_tvdb_episode(media):
        return none_response

    settings_row = (await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )).scalar_one_or_none()
    if media.media_type == MediaType.movie:
        opted_in = bool(settings_row and settings_row.rate_prompt_movies)
    elif media.media_type == MediaType.episode:
        opted_in = bool(settings_row and settings_row.rate_prompt_episodes)
    else:
        opted_in = False
    if not opted_in:
        return none_response

    recent_completion = (await db.execute(
        select(WatchEvent.id).where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.media_id == media_id,
            WatchEvent.completed == True,
            WatchEvent.watched_at >= datetime.utcnow() - RATE_PROMPT_WINDOW,
        ).limit(1)
    )).scalar_one_or_none()
    if recent_completion is None:
        return none_response

    # Movie and episode ratings are both stored with season_number NULL
    # (submit_rating collapses an episode's season to None), so one check covers both.
    already_rated = (await db.execute(
        select(Rating.id).where(
            Rating.media_id == media_id,
            Rating.user_id == current_user.id,
            Rating.season_number.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if already_rated is not None:
        return none_response

    show = media.show if media.media_type == MediaType.episode else None
    # Episodes: the still (media.poster_path) shown landscape in the popup, show
    # poster as the fallback. Movies: their own poster.
    poster = media.poster_path or (show.poster_path if show else None)
    return {
        "should_prompt": True,
        "media": {
            "id": media.id,
            "tmdb_id": media.tmdb_id,
            "type": media.media_type.value,
            "title": media.title,
            "show_title": show.title if show else None,
            "season_number": media.season_number,
            "episode_number": media.episode_number,
            "poster_path": poster,
        },
    }


@router.get("/continue-watching")
async def get_continue_watching(
    limit: int | None = Query(20),
    include_hidden: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Items currently in progress."""
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    dropped_movie_ids = set(settings.dropped_movies or []) if settings else set()

    filters = [PlaybackProgress.user_id == current_user.id]
    if dropped_movie_ids and not include_hidden:
        filters.append(Media.id.notin_(dropped_movie_ids))
    query = (
        select(PlaybackProgress, Media)
        .join(Media, Media.id == PlaybackProgress.media_id)
        .options(selectinload(PlaybackProgress.media).selectinload(Media.show))
        .where(*filters)
        .order_by(desc(PlaybackProgress.updated_at))
    )
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    rows = result.all()
    items = [format_event(e, m) for e, m in rows]
    for item in items:
        # Only ever true for movies - episodes have no drop concept.
        item["media"]["dropped"] = item["media"]["id"] in dropped_movie_ids
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
    current_user: User = Depends(get_current_user_or_api_key),
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
        "runtime": _effective_runtime(media),
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


# Shows whose stored status says they're finished can never gain a new episode.
# Revivals (e.g. Futurama) are caught by main.py's daily metadata sweep flipping
# this status back within a day (#307).
FINAL_STATUSES = {"Ended", "Canceled"}

# How stale a show's tmdb_data snapshot may get before Next Up stops trusting it
# and re-fetches live. The daily sweep rewrites every snapshot roughly every
# 24h, so while it's running this threshold is never reached and the request
# path stays fetch-free; if the sweep is down, each show degrades to one live
# fetch per ~26h instead of the snapshot going permanently stale (#287).
_NEXT_UP_SNAPSHOT_MAX_AGE = timedelta(hours=26)


def _next_up_needs_live_fetch(show, today: date, now: datetime) -> bool:
    """Whether get_next_up's missing-episode fallback must ask TMDB about this
    show, or can answer from the tmdb_data snapshot on the Show row (#332).

    The per-request question is only "has an episode appeared after the user's
    furthest-watched position?" - and the snapshot already knows when that
    can't have happened:
      - a finished show can't gain episodes (revivals flip its status via the
        daily sweep, #307);
      - a still-running show announces its next episode's air date ahead of
        time (next_episode_to_air) - until that date arrives, nothing new can
        have aired;
      - a snapshot refreshed within ~a day with no scheduled next episode
        means TMDB had nothing new either.
    Everything else - a next episode whose air date has arrived, or a
    never-written or stale snapshot - gets a live fetch, so unlike #287 a
    snapshot can never go permanently stale: it is refreshed by the daily
    sweep, and re-fetched here the moment it's overdue.
    """
    if not show or not show.tmdb_id:
        # No TMDB identity - there's nothing to fetch (unchanged behavior:
        # unmapped-TVDB shows get their episodes from TVDB sync, not here).
        return False
    data = show.tmdb_data or {}
    if data.get("source") == "tvdb":
        # TVDB-sourced snapshot: its season layout is TVDB-shaped (#335) and
        # must never be fetched-over / rewritten with TMDB-shaped data here.
        return False
    if (show.status or "") in FINAL_STATUSES:
        # A finished show's season list can't change. "seasons" missing
        # entirely means some path set the status without ever storing a
        # snapshot - fetch once to build it.
        return "seasons" not in data
    next_air = (data.get("next_episode_to_air") or {}).get("air_date")
    if next_air and next_air <= today.isoformat():
        return True
    refreshed_at = data.get("refreshed_at")
    if not refreshed_at:
        return True
    try:
        refreshed = datetime.fromisoformat(refreshed_at)
    except (TypeError, ValueError):
        return True
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    return now - refreshed > _NEXT_UP_SNAPSHOT_MAX_AGE


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


def _progress_percentages(
    season_ep_counts: dict[int, int],
    watched_per_season: dict[int, int],
    collected_per_season: dict[int, int],
) -> dict | None:
    """Watched / collected episode counts and percentages for one show's
    Progress row (#374). season_ep_counts is capped_season_episode_counts
    output; per-season counts are themselves capped at that so a provider
    numbering mismatch (more local rows than TMDB says a season has) can't
    push a percentage past 100. Specials (season 0) are excluded, matching
    total_aired_episodes. Returns None for a show with no usable season
    metadata at all - a "0 / 0" row is just noise.
    """
    total = sum(cnt for sn, cnt in season_ep_counts.items() if sn != 0)
    if total == 0:
        return None
    watched = sum(
        min(watched_per_season.get(sn, 0), cnt)
        for sn, cnt in season_ep_counts.items() if sn != 0
    )
    collected = sum(
        min(collected_per_season.get(sn, 0), cnt)
        for sn, cnt in season_ep_counts.items() if sn != 0
    )
    return {
        "episodes_total": total,
        "episodes_watched": watched,
        "episodes_collected": collected,
        "watch_pct": min(100, round(watched / total * 100)),
        "collection_pct": min(100, round(collected / total * 100)),
    }


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

    # Grouped by show_id up front so the per-show lookup below is a single dict
    # access instead of a full-dict filter repeated once per show.
    watched_per_by_show: dict[int, dict[int, int]] = {}
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
            watched_per_by_show.setdefault(sid, {})[sn] = cnt

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
            watched_per_by_show.setdefault(sid, {})[sn] = cnt

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

    # Shows still airing whose cached metadata has no last_episode_to_air can't
    # be capped to aired episodes, so the count would include episodes that
    # haven't aired yet (#296). Fetch it live for exactly those shows: Next Up
    # is a short list, and finished shows never need it.
    needs_last_ep = [
        sid for sid, show in shows_by_id.items()
        if show.tmdb_id
        and not (show.tmdb_data or {}).get("last_episode_to_air")
        and (show.status or "") not in FINAL_STATUSES
    ]
    last_ep_by_show: dict[int, dict] = {}
    if needs_last_ep:
        tmdb_key = await get_user_tmdb_key(db, user_id)
        if check_tmdb_key(tmdb_key):
            # Next Up's full-page view (unlike the home widget) has no limit,
            # so needs_last_ep can be every still-airing show a user follows -
            # cap fan-out concurrency instead of firing one request per show
            # at once (same pattern as routers/calendar.py's FETCH_CONCURRENCY).
            sem = asyncio.Semaphore(8)

            async def _fetch_show_light(sid: int) -> tuple[int, dict | None]:
                async with sem:
                    try:
                        return sid, await tmdb.get_show_light(shows_by_id[sid].tmdb_id, api_key=tmdb_key)
                    except Exception:
                        return sid, None

            # Cap the whole fan-out: this only refines capped-season counts, so
            # if TMDB is slow/down we render Next Up from stored metadata rather
            # than block the home page. tmdb._get's breaker makes calls after the
            # first failure instant regardless.
            try:
                fetched_last_ep = await asyncio.wait_for(
                    asyncio.gather(*[_fetch_show_light(sid) for sid in needs_last_ep]),
                    timeout=6.0,
                )
            except (asyncio.TimeoutError, tmdb.TMDBUnavailable):
                fetched_last_ep = []
            for sid, data in fetched_last_ep:
                if data:
                    last_ep_by_show[sid] = data

    stats: dict[int, dict] = {}
    for sid, show in shows_by_id.items():
        season_counts = capped_season_episode_counts(show, last_ep_by_show.get(sid))
        avg_runtime = avg_by_show.get(sid)
        if not avg_runtime:
            run_times = (show.tmdb_data or {}).get("episode_run_time") or []
            avg_runtime = run_times[0] if run_times else None
        per_season = watched_per_by_show.get(sid, {})
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
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    dropped_show_ids = set(settings.dropped_shows or []) if settings else set()

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
    #
    # Dropped shows are excluded from this fallback specifically (not from
    # last_per_show/next_per_show above) - the point of dropping is to stop
    # paying attention to a show, so it shouldn't cost a live TMDB fetch on
    # every Next Up load just to compute a candidate nobody will see by
    # default (#117 follow-up). A dropped show that already has a locally
    # synced next-episode row still surfaces fine under "Show hidden" below;
    # only the on-demand computation is skipped for it.
    missing_show_ids = set(last_per_show) - set(next_per_show) - dropped_show_ids
    if missing_show_ids:
        api_key = await get_user_tmdb_key(db, current_user.id)
        if check_tmdb_key(api_key):
            shows_result = await db.execute(select(Show).where(Show.id.in_(missing_show_ids)))
            shows_by_id = {s.id: s for s in shows_result.scalars().all()}

            # A show with no locally-synced next episode is commonly just a
            # fully-watched one, so a user with many completed shows can have
            # hundreds of these on every cold Next Up load - fetched live
            # per-request, that fan-out was the entire multi-minute cold-load
            # cost (#294, #307, #332). Only shows whose stored metadata says
            # something new could exist since it was last written are fetched
            # (see _next_up_needs_live_fetch); everything else is answered
            # from the tmdb_data snapshot, which main.py's daily metadata
            # sweep keeps at most ~a day old - so unlike #287, trusting it
            # here can't hide a newly-aired episode for good.
            fetch_today = date.today()
            fetch_now = datetime.now(timezone.utc)
            live_fetch_ids = [
                sid for sid in missing_show_ids
                if _next_up_needs_live_fetch(shows_by_id.get(sid), fetch_today, fetch_now)
            ]

            fetched_by_show: dict[int, dict] = {}
            if live_fetch_ids:
                _fetch_sem = asyncio.Semaphore(10)

                async def _fetch_show(show_id: int) -> tuple[int, dict | None]:
                    async with _fetch_sem:
                        try:
                            return show_id, await tmdb.get_show(shows_by_id[show_id].tmdb_id, api_key=api_key)
                        except Exception:
                            return show_id, None

                # Cap the whole fan-out: if TMDB is slow/down, render Next Up
                # from the stored snapshots rather than block the home page
                # (the per-show except above falls back the same way).
                try:
                    fetch_results = await asyncio.wait_for(
                        asyncio.gather(*[_fetch_show(sid) for sid in live_fetch_ids]),
                        timeout=8.0,
                    )
                except (asyncio.TimeoutError, tmdb.TMDBUnavailable):
                    fetch_results = []
                fetched_by_show = {sid: data for sid, data in fetch_results if data}

            from routers.shows import apply_show_metadata

            for show_id in missing_show_ids:
                show = shows_by_id.get(show_id)
                if not show or not show.tmdb_id:
                    continue
                fresh_show_data = fetched_by_show.get(show_id)
                if fresh_show_data is not None:
                    # Persist the fresh payload (incl. next_episode_to_air and
                    # refreshed_at) so this show stops needing a live fetch on
                    # the next load - committed together with the speculative
                    # episode rows below. Safe to write: TVDB-sourced
                    # snapshots are never in live_fetch_ids (#335).
                    apply_show_metadata(show, fresh_show_data)
                    seasons = fresh_show_data.get("seasons", [])
                else:
                    seasons = (show.tmdb_data or {}).get("seasons", [])
                season, episode = last_per_show[show_id]
                next_ep = _compute_next_episode(seasons, season, episode)
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

    hidden_set = set(settings.next_up_hidden_shows or []) if settings else set()

    today = date.today()
    next_up = [
        m for m in next_per_show.values()
        if m.id not in completed_ids
        and (include_hidden or (m.show_id not in hidden_set and m.show_id not in dropped_show_ids))
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
        item["dropped"] = item.get("show_id") in dropped_show_ids
        # The show's most recent watch (or, mid-rewatch, the rewatch's own
        # progress/start time) - already computed above for the sort just
        # below, just not previously returned. Lets a client interleave this
        # feed with /continue-watching (whose items already carry a
        # comparable top-level watched_at) into one activity-sorted row, the
        # way Stremio/Nuvio-style addons do (#237).
        show_last_watched_at = last_watched_at.get(item.get("show_id"))
        item["last_watched_at"] = show_last_watched_at.isoformat() if show_last_watched_at else None
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm.attributes import flag_modified


class NextUpHideRequest(BaseModel):
    show_id: int


@router.post("/next-up/hide")
async def hide_next_up_show(
    body: NextUpHideRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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


# Concurrency for the "Refresh from TMDB" fan-out. TMDB's rate limit is ~50
# req/s; 10 in flight with typical latency stays comfortably under it.
_NEXT_UP_REFRESH_CONCURRENCY = 10
# Commit every N completed shows so a mid-refresh disconnect (big library, slow
# TMDB) still persists most of the work instead of throwing all of it away.
_NEXT_UP_REFRESH_COMMIT_EVERY = 20


async def _stream_next_up_refresh(user_id: int, api_key: str):
    """Newline-delimited-JSON progress stream for the Next Up "Refresh from
    TMDB" button. Re-queries TMDB (cache bypassed) for every TMDB-backed show
    the user has watch history for and rewrites its stored tmdb_data snapshot,
    so the next plain Next Up load surfaces episodes/seasons that have appeared
    since the last daily metadata sweep (main.py's _show_metadata_refresher).

    Emits, one JSON object per line:
        {"total": N}
        {"done": 1, "total": N} ... {"done": N, "total": N}
        {"done": N, "total": N, "complete": true}
    or, when there's nothing to do:
        {"total": 0, "complete": true[, "error": "no_tmdb_key"]}
    """
    if not check_tmdb_key(api_key):
        yield json.dumps({"total": 0, "complete": True, "error": "no_tmdb_key"}) + "\n"
        return

    async with AsyncSessionLocal() as db:
        # Shows the user has actually watched an episode of, that carry a TMDB
        # identity. TVDB-sourced snapshots are excluded: their season layout is
        # TVDB-shaped (#335) and apply_show_metadata would clobber it.
        watched_show_ids = (
            select(Media.show_id)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(
                WatchEvent.user_id == user_id,
                Media.media_type == MediaType.episode,
                Media.show_id.isnot(None),
            )
            .distinct()
        )
        show_rows = (await db.execute(
            select(Show).where(Show.tmdb_id.isnot(None), Show.id.in_(watched_show_ids))
        )).scalars().all()
        shows = [s for s in show_rows if (s.tmdb_data or {}).get("source") != "tvdb"]

        total = len(shows)
        yield json.dumps({"total": total}) + "\n"
        if not total:
            yield json.dumps({"done": 0, "total": 0, "complete": True}) + "\n"
            return

        from routers.shows import apply_show_metadata

        sem = asyncio.Semaphore(_NEXT_UP_REFRESH_CONCURRENCY)
        queue: asyncio.Queue = asyncio.Queue()

        async def _one(show):
            async with sem:
                try:
                    # cache_ttl=None: the whole point of the button is that the
                    # cached/snapshotted data is behind, so don't reuse it.
                    data = await tmdb.get_show(show.tmdb_id, api_key=api_key, cache_ttl=None)
                    apply_show_metadata(show, data)
                except Exception:
                    pass
            await queue.put(1)

        tasks = [asyncio.create_task(_one(s)) for s in shows]
        done = 0
        try:
            for _ in range(total):
                await queue.get()
                done += 1
                if done % _NEXT_UP_REFRESH_COMMIT_EVERY == 0:
                    await db.commit()
                yield json.dumps({"done": done, "total": total}) + "\n"
        finally:
            # On a normal finish every task is already done and these are
            # no-ops; on a client disconnect mid-stream they stop the rest of
            # the fan-out instead of letting an abandoned request keep hammering
            # TMDB. Either way, commit what did land.
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await db.commit()

        yield json.dumps({"done": total, "total": total, "complete": True}) + "\n"


@router.post("/next-up/refresh")
async def refresh_next_up(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Streams NDJSON progress while re-checking TMDB for every show the user
    watches (see _stream_next_up_refresh). The client reloads Next Up when the
    stream completes; the refreshed snapshots are what let a previously-missing
    show surface on that reload."""
    api_key = await get_user_tmdb_key(db, current_user.id)
    return StreamingResponse(
        _stream_next_up_refresh(current_user.id, api_key),
        media_type="application/x-ndjson",
        # Defeat proxy/buffering so progress lines arrive as they're produced.
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


async def _push_show_dropped_to_providers(db: AsyncSession, settings: UserSettings, tmdb_id: int, *, remove: bool) -> None:
    """Best-effort push of a show's dropped state to Trakt/MDBList - never
    raises, mirrors _push_list_item_to_trakt's error-swallowing pattern
    (routers/lists.py) since a failed provider push shouldn't block the local
    drop/undrop action itself."""
    if settings.trakt_push_dropped and settings.trakt_access_token and settings.trakt_client_id:
        try:
            from routers.trakt import ensure_valid_trakt_token
            token = await ensure_valid_trakt_token(db, settings)
            if remove:
                await trakt_client.remove_from_hidden(settings.trakt_client_id, token, "dropped", tmdb_id)
            else:
                await trakt_client.add_to_hidden(settings.trakt_client_id, token, "dropped", tmdb_id)
        except Exception as exc:
            logger.warning("Failed to push dropped show to Trakt (tmdb_id=%s, remove=%s): %s", tmdb_id, remove, exc)

    if settings.mdblist_push_dropped and settings.mdblist_api_key:
        from core import mdblist as mdblist_client
        try:
            if remove:
                await mdblist_client.remove_dropped(settings.mdblist_api_key, tmdb_id)
            else:
                await mdblist_client.push_dropped(settings.mdblist_api_key, tmdb_id, _iso_utc_now())
        except Exception as exc:
            logger.warning("Failed to push dropped show to MDBList (tmdb_id=%s, remove=%s): %s", tmdb_id, remove, exc)


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DropShowRequest(BaseModel):
    show_id: int


@router.post("/drop/show")
async def drop_show(
    body: DropShowRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Drop a show: excluded from Next Up, Calendar, and Discover/
    recommendations, without touching watch history (#117)."""
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found")
    dropped = list(settings.dropped_shows or [])
    if body.show_id not in dropped:
        dropped.append(body.show_id)
        settings.dropped_shows = dropped
        flag_modified(settings, "dropped_shows")
        await db.commit()

    show_result = await db.execute(select(Show).where(Show.id == body.show_id))
    show = show_result.scalar_one_or_none()
    if show and show.tmdb_id:
        await _push_show_dropped_to_providers(db, settings, show.tmdb_id, remove=False)
    return {"status": "ok"}


@router.delete("/drop/show")
async def undrop_show(
    show_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found")
    dropped = list(settings.dropped_shows or [])
    if show_id in dropped:
        dropped.remove(show_id)
        settings.dropped_shows = dropped
        flag_modified(settings, "dropped_shows")
        await db.commit()

    show_result = await db.execute(select(Show).where(Show.id == show_id))
    show = show_result.scalar_one_or_none()
    if show and show.tmdb_id:
        await _push_show_dropped_to_providers(db, settings, show.tmdb_id, remove=True)
    return {"status": "ok"}


class DropMovieRequest(BaseModel):
    # dropped_movies stores local Media.id. Callers with a local row send it
    # directly; a movie opened straight from TMDB (not watched/collected) has
    # no local row yet, so it sends tmdb_id and we resolve/create one (#330).
    media_id: int | None = None
    tmdb_id: int | None = None


async def _resolve_movie_media_id(
    db: AsyncSession, user_id: int, media_id: int | None, tmdb_id: int | None, *, create: bool
) -> int | None:
    """Turn a (media_id | tmdb_id) drop/undrop request into a local movie
    Media.id. create=True materialises the row from TMDB when it doesn't
    exist yet (drop); create=False just looks (undrop - nothing to remove
    if there's no row)."""
    if media_id and media_id > 0:
        return media_id
    if not tmdb_id:
        return None
    existing = (await db.execute(
        select(Media)
        .where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.movie)
        .order_by(Media.id)
    )).scalars().first()
    if existing:
        return existing.id
    if not create:
        return None
    from routers.media import get_user_tmdb_key
    api_key = await get_user_tmdb_key(db, user_id)
    try:
        data = await tmdb.get_movie(tmdb_id, api_key=api_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Movie not found on TMDB: {e}")
    media, _created = await create_media_safely(db, tmdb_id, MediaType.movie, title=data.get("title") or "")
    await enrich_media(media, api_key=api_key)
    await db.flush()
    return media.id


@router.post("/drop/movie")
async def drop_movie(
    body: DropMovieRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Drop a movie: excluded from Continue Watching and Discover/
    recommendations, without touching watch history (#117). Neither Trakt
    nor MDBList support a dropped-movie concept, so this is local-only.

    The underlying PlaybackProgress row is left alone rather than deleted -
    get_continue_watching already filters it out by dropped_movies, and
    keeping it means the resume position survives if the movie is later
    undropped, instead of being lost."""
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found")
    media_id = await _resolve_movie_media_id(db, current_user.id, body.media_id, body.tmdb_id, create=True)
    if not media_id:
        raise HTTPException(status_code=400, detail="media_id or tmdb_id is required")
    dropped = list(settings.dropped_movies or [])
    if media_id not in dropped:
        dropped.append(media_id)
        settings.dropped_movies = dropped
        flag_modified(settings, "dropped_movies")
        await db.commit()
    return {"status": "ok"}


@router.delete("/drop/movie")
async def undrop_movie(
    media_id: int | None = Query(None),
    tmdb_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if settings:
        resolved = await _resolve_movie_media_id(db, current_user.id, media_id, tmdb_id, create=False)
        dropped = list(settings.dropped_movies or [])
        if resolved is not None and resolved in dropped:
            dropped.remove(resolved)
            settings.dropped_movies = dropped
            flag_modified(settings, "dropped_movies")
            await db.commit()
    return {"status": "ok"}


@router.get("/dropped")
async def list_dropped(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Every dropped show and movie, for the dedicated Dropped page (#330).

    dropped_shows / dropped_movies store bare local ids with no timestamps;
    they're returned newest-drop-first (drop appends to the list)."""
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    show_ids = list(settings.dropped_shows or []) if settings else []
    movie_ids = list(settings.dropped_movies or []) if settings else []

    shows_out: list[dict] = []
    if show_ids:
        rows = (await db.execute(select(Show).where(Show.id.in_(show_ids)))).scalars().all()
        by_id = {s.id: s for s in rows}
        for sid in reversed(show_ids):
            s = by_id.get(sid)
            if not s:
                continue
            shows_out.append({
                "id": s.id,
                "tmdb_id": s.tmdb_id,
                "tvdb_id": s.tvdb_id,
                "title": s.title,
                "poster_path": s.poster_path,
                "year": (s.first_air_date or "")[:4] or None,
                "status": s.status,
            })

    movies_out: list[dict] = []
    if movie_ids:
        rows = (await db.execute(
            select(Media).where(Media.id.in_(movie_ids), Media.media_type == MediaType.movie)
        )).scalars().all()
        by_id = {m.id: m for m in rows}
        for mid in reversed(movie_ids):
            m = by_id.get(mid)
            if not m:
                continue
            movies_out.append({
                "id": m.id,
                "tmdb_id": m.tmdb_id,
                "title": m.title,
                "poster_path": m.poster_path,
                "year": (m.release_date or "")[:4] or None,
            })

    return {"shows": shows_out, "movies": movies_out}


_PROGRESS_SORTS = (
    "recent", "watch_asc", "watch_desc",
    "collection_asc", "collection_desc", "remaining", "title",
)


@router.get("/progress")
async def get_progress(
    type: str = Query("watched", pattern="^(watched|collection)$"),
    sort: str = Query("recent"),
    hide_complete: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Per-show progress for the Progress dashboard (#374), as two independent
    lists picked by `type`:

    - `watched`: every show with at least one watched episode.
    - `collection`: every show with at least one collected episode.

    The two are deliberately not linked - a show you have every episode of
    but have never watched belongs on the collection list and nowhere near
    the watched one, and vice versa. Both `watch_pct` and `collection_pct`
    are still returned for each row as context. Dropped shows are left out
    (same as Next Up); sorting and the hide-completed filter are client-side.

    Episode totals come from stored TMDB metadata only (capped at the last
    aired episode, unaired seasons dropped by their premiere date) - no live
    TMDB call per show, so a still-airing show whose cached metadata lags may
    briefly read a few episodes short. That's the same trade-off
    total_aired_episodes() already documents.
    """
    user_id = current_user.id
    settings = (await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )).scalar_one_or_none()
    dropped_show_ids = set(settings.dropped_shows or []) if settings else set()

    if type == "collection":
        membership_join = (Collection, Collection.media_id == Media.id)
        membership_where = Collection.user_id == user_id
    else:
        membership_join = (WatchEvent, WatchEvent.media_id == Media.id)
        membership_where = WatchEvent.user_id == user_id

    started_result = await db.execute(
        select(func.distinct(Media.show_id))
        .join(*membership_join)
        .where(
            membership_where,
            Media.media_type == MediaType.episode,
            Media.show_id.isnot(None),
        )
    )
    show_ids = {row[0] for row in started_result.all()} - dropped_show_ids
    if not show_ids:
        return {"shows": [], "page": 1, "page_size": page_size, "total": 0, "total_pages": 1}

    shows_by_id = {
        s.id: s for s in (
            await db.execute(select(Show).where(Show.id.in_(show_ids)))
        ).scalars().all()
    }

    # One pass over every episode row of these shows: distinct watched and
    # distinct collected episode numbers per season (same shape the show
    # detail page's season_stats query produces, just batched across shows).
    watch_a = aliased(WatchEvent)
    coll_a = aliased(Collection)
    season_rows = await db.execute(
        select(
            Media.show_id,
            Media.season_number,
            func.count(func.distinct(
                case((watch_a.id.isnot(None), Media.episode_number), else_=None)
            )),
            func.count(func.distinct(
                case((coll_a.id.isnot(None), Media.episode_number), else_=None)
            )),
        )
        .select_from(Media)
        .outerjoin(watch_a, and_(watch_a.media_id == Media.id, watch_a.user_id == user_id))
        .outerjoin(coll_a, and_(coll_a.media_id == Media.id, coll_a.user_id == user_id))
        .where(
            Media.show_id.in_(show_ids),
            Media.media_type == MediaType.episode,
            Media.season_number.isnot(None),
            Media.episode_number.isnot(None),
        )
        .group_by(Media.show_id, Media.season_number)
    )
    watched_per_show: dict[int, dict[int, int]] = {}
    collected_per_show: dict[int, dict[int, int]] = {}
    for sid, season_number, watched_count, collected_count in season_rows.all():
        watched_per_show.setdefault(sid, {})[season_number] = watched_count
        collected_per_show.setdefault(sid, {})[season_number] = collected_count

    last_watched_rows = await db.execute(
        select(Media.show_id, func.max(WatchEvent.watched_at))
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.show_id.in_(show_ids))
        .group_by(Media.show_id)
    )
    last_watched = {sid: ts for sid, ts in last_watched_rows.all()}

    shows_out: list[dict] = []
    for sid, show in shows_by_id.items():
        stats = _progress_percentages(
            capped_season_episode_counts(show),
            watched_per_show.get(sid, {}),
            collected_per_show.get(sid, {}),
        )
        if stats is None:
            continue
        ts = last_watched.get(sid)
        shows_out.append({
            "show_id": sid,
            "tmdb_id": show.tmdb_id,
            "tvdb_id": show.tvdb_id,
            "title": show.title,
            "poster_path": show.poster_path,
            "status": show.status,
            **stats,
            "last_watched_at": ts.isoformat() if ts else None,
        })

    done_key = "episodes_collected" if type == "collection" else "episodes_watched"
    pct_key = "collection_pct" if type == "collection" else "watch_pct"

    # A show reaches the "started" query above on any episode row - including a
    # Special, or an episode in a season the cached TMDB metadata doesn't list.
    # _progress_percentages counts neither, so those would show as "0 / 85".
    # Keep a row only if it has real progress on this list's own metric.
    shows_out = [s for s in shows_out if s[done_key] > 0]

    if hide_complete:
        shows_out = [s for s in shows_out if s[pct_key] < 100]

    # "remaining" is episodes left to reach 100% of whichever list this is.
    def _remaining(s: dict) -> int:
        return s["episodes_total"] - s[done_key]

    if sort not in _PROGRESS_SORTS:
        sort = "recent"
    sort_keys = {
        "recent": (lambda s: s["last_watched_at"] or "", True),
        "watch_asc": (lambda s: s["watch_pct"], False),
        "watch_desc": (lambda s: s["watch_pct"], True),
        "collection_asc": (lambda s: s["collection_pct"], False),
        "collection_desc": (lambda s: s["collection_pct"], True),
        "remaining": (_remaining, True),
        "title": (lambda s: s["title"].lower(), False),
    }
    key_fn, reverse = sort_keys[sort]
    # Stable secondary sort by title so equal percentages have a predictable order.
    shows_out.sort(key=lambda s: s["title"].lower())
    shows_out.sort(key=key_fn, reverse=reverse)

    total = len(shows_out)
    total_pages = max(1, (total + page_size - 1) // page_size)
    offset = (page - 1) * page_size
    return {
        "shows": shows_out[offset:offset + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


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
    current_user: User = Depends(get_current_user_or_api_key),
):
    # A watch event is only ever a movie or an episode. Marking a whole show or
    # season watched is a bulk-of-episodes operation and has its own endpoints
    # (/history/show-all, /history/season) - accepting media_type=series here
    # would create a bogus series-level watch event. See #358.
    if event_in.media_type not in (MediaType.movie, MediaType.episode):
        raise HTTPException(
            status_code=422,
            detail="media_type must be 'movie' or 'episode'; use /history/show-all or /history/season to mark a show or season watched",
        )

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
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
):
    # Any active rewatch is meaningless once its underlying history is gone -
    # RewatchProgress cascades from WatchEvent deletion already, but the
    # ShowRewatch "currently rewatching" marker itself doesn't depend on any
    # WatchEvent and would otherwise survive a full clear, stuck at 0
    # progress forever with nothing left to progress it.
    await db.execute(delete(ShowRewatch).where(ShowRewatch.user_id == current_user.id))
    await db.execute(delete(WatchEvent).where(WatchEvent.user_id == current_user.id))
    # Continue Watching is sourced from PlaybackProgress, not WatchEvent -
    # without this, in-progress items kept showing up there after a clear.
    await db.execute(delete(PlaybackProgress).where(PlaybackProgress.user_id == current_user.id))
    await db.commit()
    return {"status": "ok", "message": "Watch history cleared"}


@router.delete("/item")
async def unwatch_item(
    tmdb_id: int | None = Query(None),
    media_id: int | None = Query(None, alias="id"),
    media_type: MediaType = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
                "last_episode_to_air": data.get("last_episode_to_air"),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
        # Episode: create a minimal row from request data. If the show isn't
        # in the local DB yet - e.g. starting a manual session from an
        # episode page for a show never added to Scrob before - resolve or
        # create it now instead of only ever looking one up. Without this,
        # the episode's Media row ends up with no show_id, and the Now
        # Playing bar (which sources an episode's title/poster/link from its
        # linked Show) shows a blank poster, the bare episode title, and no
        # link at all (#366 - same root cause class as #192's webhook
        # fast-path gap, just on the manual-session path instead).
        show = None
        if body.show_tmdb_id:
            show_q = await db.execute(select(Show).where(Show.tmdb_id == body.show_tmdb_id))
            show = show_q.scalar_one_or_none()
            if not show and check_tmdb_key(api_key):
                from routers.webhooks import _find_or_create_show
                try:
                    show = await _find_or_create_show(db, body.show_tmdb_id, api_key)
                except Exception:
                    show = None
        media, _created = await create_media_safely(
            db,
            body.tmdb_id,
            body.media_type,
            title=body.title or "Unknown",
            runtime=body.runtime,
            season_number=body.season_number,
            episode_number=body.episode_number,
            show_id=show.id if show else None,
        )
        if show is not None and media.show_id is None:
            media.show_id = show.id

    return media


@router.post("/session/start")
async def start_manual_session(
    body: schemas.ManualSessionStart,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Start a manual scrobble session for any movie or episode."""
    # A session completes into a watch event, which is movie/episode only - a
    # series-type session would produce a bogus series-level watch event. #358.
    if body.media_type not in (MediaType.movie, MediaType.episode):
        raise HTTPException(status_code=422, detail="media_type must be 'movie' or 'episode'")

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
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
    current_user: User = Depends(get_current_user_or_api_key),
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
