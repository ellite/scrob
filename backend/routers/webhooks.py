import json
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, or_
from sqlalchemy.orm.exc import StaleDataError

from db import get_db
from models.media import Media
from models.show import Show
from models.collection import Collection, CollectionFile
from models.events import WatchEvent
from models.ratings import Rating
from models.users import User, UserSettings
from models.global_settings import GlobalSettings
from models.connections import MediaServerConnection
from models.scrobble_connection import ScrobbleConnection
from models.base import MediaType, CollectionSource
from models.playback_session import PlaybackSession
from models.playback_progress import PlaybackProgress
from models.library_selections import PlexLibrarySelection, JellyfinLibrarySelection, EmbyLibrarySelection
from core.enrichment import create_media_safely, enrich_media, enrich_media_safely
from core.rewatch import record_rewatch_progress, get_active_rewatch
from models.rewatch import RewatchProgress
from core import tmdb
from core import trakt as trakt_client
from core import simkl as simkl_client
from core import mdblist as mdblist_client
from core import tvdb as tvdb_client
from core.jellyfin import extract_quality
from core.translations import get_user_metadata_language

router = APIRouter()


async def _maybe_trakt_scrobble(
    settings: UserSettings | None,
    media: "Media",
    action: str,
    progress_percent: float,
    db: AsyncSession | None = None,
) -> None:
    """Forward a play/pause/stop event to Trakt's scrobble API. Errors are swallowed."""
    if not (settings and settings.trakt_scrobble and settings.trakt_access_token and settings.trakt_client_id):
        return

    from sqlalchemy import inspect as sa_inspect

    progress = min(100.0, round(progress_percent * 100, 1))

    try:
        if media.media_type == MediaType.movie:
            year: int | None = None
            if media.release_date:
                try:
                    year = int(str(media.release_date)[:4])
                except (ValueError, TypeError):
                    pass
            await trakt_client.scrobble_movie(
                settings.trakt_client_id, settings.trakt_access_token,
                action=action,
                tmdb_id=media.tmdb_id,
                progress=progress,
                title=media.title,
                year=year,
            )
        elif media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
            state = sa_inspect(media)
            if "show" in state.unloaded:
                show = await db.get(Show, media.show_id) if db and media.show_id else None
            else:
                show = media.show
            await trakt_client.scrobble_episode(
                settings.trakt_client_id, settings.trakt_access_token,
                action=action,
                season_number=media.season_number,
                episode_number=media.episode_number,
                progress=progress,
                show_tmdb_id=show.tmdb_id if show else None,
                show_title=show.title if show else None,
                episode_tmdb_id=media.tmdb_id,
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("[Trakt scrobble] %s failed: %s", action, exc)


async def _maybe_simkl_scrobble(
    settings: UserSettings | None,
    media: "Media",
    action: str,
    progress_percent: float = 0.0,
    db: AsyncSession | None = None,
) -> None:
    """Forward a play/stop event to Simkl's scrobble API. Errors are swallowed.
    action is 'start' (play/resume) or 'stop'. Simkl has no pause concept.
    'start' fires a fire-and-forget checkin (Simkl runtime-extrapolates progress
    from there); 'stop' calls /scrobble/stop with the real progress, since
    there's no separate cancel/delete endpoint to end a checkin otherwise."""
    if not (settings and settings.simkl_scrobble and settings.simkl_access_token and settings.simkl_client_id):
        return

    from sqlalchemy import inspect as sa_inspect

    progress = min(100.0, round(progress_percent * 100, 1))

    try:
        if media.media_type == MediaType.movie and media.tmdb_id:
            if action == "start":
                year: int | None = None
                if media.release_date:
                    try:
                        year = int(str(media.release_date)[:4])
                    except (ValueError, TypeError):
                        pass
                await simkl_client.checkin_movie(
                    settings.simkl_client_id, settings.simkl_access_token,
                    tmdb_id=media.tmdb_id,
                    title=media.title,
                    year=year,
                    progress=progress,
                )
            elif action == "stop":
                await simkl_client.stop_scrobble_movie(
                    settings.simkl_client_id, settings.simkl_access_token,
                    tmdb_id=media.tmdb_id,
                    progress=progress,
                )
        elif media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
            state = sa_inspect(media)
            if "show" in state.unloaded:
                show = await db.get(Show, media.show_id) if db and media.show_id else None
            else:
                show = media.show
            if show and show.tmdb_id:
                if action == "start":
                    await simkl_client.checkin_episode(
                        settings.simkl_client_id, settings.simkl_access_token,
                        show_tmdb_id=show.tmdb_id,
                        season_number=media.season_number,
                        episode_number=media.episode_number,
                        show_title=show.title,
                        progress=progress,
                    )
                elif action == "stop":
                    await simkl_client.stop_scrobble_episode(
                        settings.simkl_client_id, settings.simkl_access_token,
                        show_tmdb_id=show.tmdb_id,
                        season_number=media.season_number,
                        episode_number=media.episode_number,
                        progress=progress,
                    )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("[Simkl scrobble] %s failed: %s", action, exc)


async def _maybe_mdblist_scrobble(
    settings: UserSettings | None,
    media: "Media",
    action: str,
    progress_percent: float,
    db: AsyncSession | None = None,
) -> None:
    """Forward a play/pause/stop event to MDBList's scrobble API. Errors are swallowed."""
    if not (settings and settings.mdblist_scrobble and settings.mdblist_api_key):
        return

    from sqlalchemy import inspect as sa_inspect

    progress = min(100.0, round(progress_percent * 100, 1))

    # MDBList's /scrobble/stop downgrades any sub-80% stop into a resumable "paused"
    # session (same threshold Trakt uses). Below our own "did they actually watch
    # anything" cutoff, also clear the session so a barely-started play doesn't leave
    # a phantom continue-watching entry.
    actions = [action]
    if action == "stop" and progress_percent <= 0.05:
        actions.append("clear")

    try:
        show = None
        if media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
            state = sa_inspect(media)
            if "show" in state.unloaded:
                show = await db.get(Show, media.show_id) if db and media.show_id else None
            else:
                show = media.show

        for act in actions:
            act_progress = progress if act != "clear" else None
            if media.media_type == MediaType.movie and media.tmdb_id:
                await mdblist_client.scrobble_movie(
                    settings.mdblist_api_key,
                    action=act,
                    tmdb_id=media.tmdb_id,
                    progress=act_progress,
                )
            elif media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
                if show and show.tmdb_id:
                    await mdblist_client.scrobble_episode(
                        settings.mdblist_api_key,
                        action=act,
                        show_tmdb_id=show.tmdb_id,
                        season_number=media.season_number,
                        episode_number=media.episode_number,
                        progress=act_progress,
                    )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("[MDBList scrobble] %s failed: %s", action, exc)


async def _maybe_bingebase_scrobble(
    settings: UserSettings | None,
    media: "Media",
    action: str,
    progress_percent: float,
    db: AsyncSession | None = None,
) -> None:
    """Forward a play/pause/stop event to Bingebase Webhook URL. Errors are swallowed."""
    if not (settings and settings.bingebase_scrobble and settings.bingebase_webhook_url):
        return

    try:
        import httpx
        from sqlalchemy import inspect as sa_inspect

        progress = min(100.0, round(progress_percent * 100, 1))
        event_name = "playback.stop" if action == "stop" else ("playback.pause" if action == "pause" else "playback.start")

        provider_ids = {}
        if media.tmdb_id:
            provider_ids["Tmdb"] = str(media.tmdb_id)
        if media.imdb_id:
            provider_ids["Imdb"] = media.imdb_id

        item_data = {
            "Name": media.title,
            "Type": "Episode" if media.media_type == MediaType.episode else "Movie",
            "ProviderIds": provider_ids,
        }

        if media.media_type == MediaType.episode:
            item_data["ParentIndexNumber"] = media.season_number
            item_data["IndexNumber"] = media.episode_number
            if media.show_id and db:
                state = sa_inspect(media)
                show = await db.get(Show, media.show_id) if "show" in state.unloaded else media.show
                if show:
                    item_data["SeriesName"] = show.title
                    if show.tmdb_id:
                        provider_ids["ShowTmdb"] = str(show.tmdb_id)

        payload = {
            "Event": event_name,
            "NotificationType": "PlaybackStop" if action == "stop" else "PlaybackStart",
            "Item": item_data,
            "Percentage": progress,
        }

        headers = {"User-Agent": "Scrob/1.0", "Content-Type": "application/json"}
        if settings.bingebase_api_key:
            headers["Authorization"] = f"Bearer {settings.bingebase_api_key}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(settings.bingebase_webhook_url, json=payload, headers=headers)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("[Bingebase scrobble] %s failed: %s", action, exc)


async def _get_tmdb_key(db: AsyncSession, settings: UserSettings | None) -> str | None:
    if settings and settings.tmdb_api_key:
        return settings.tmdb_api_key
    gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = gs_result.scalar_one_or_none()
    return gs.tmdb_api_key if gs else None


async def _get_oldest_connection(db: AsyncSession, user_id: int, conn_type: str) -> MediaServerConnection | None:
    result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == user_id,
            MediaServerConnection.type == conn_type,
        ).order_by(MediaServerConnection.id.asc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _get_connection_by_id(db: AsyncSession, user_id: int, connection_id: int) -> MediaServerConnection | None:
    result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.id == connection_id,
            MediaServerConnection.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _get_scrobble_connection_by_id(db: AsyncSession, user_id: int, connection_id: int) -> ScrobbleConnection | None:
    result = await db.execute(
        select(ScrobbleConnection).where(
            ScrobbleConnection.id == connection_id,
            ScrobbleConnection.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _find_or_create_show(db: AsyncSession, series_tmdb_id: int, api_key: str = None) -> Show:
    result = await db.execute(select(Show).where(Show.tmdb_id == series_tmdb_id))
    show = result.scalar_one_or_none()
    if not show:
        show_data = await tmdb.get_show(series_tmdb_id, api_key=api_key)
        show = Show(
            tmdb_id=series_tmdb_id,
            title=show_data.get("name", ""),
            original_title=show_data.get("original_name"),
            overview=show_data.get("overview"),
            poster_path=tmdb.poster_url(show_data.get("poster_path")),
            backdrop_path=tmdb.poster_url(show_data.get("backdrop_path"), size="w1280"),
            tmdb_rating=show_data.get("vote_average"),
            status=show_data.get("status"),
            tagline=show_data.get("tagline"),
            first_air_date=show_data.get("first_air_date"),
            last_air_date=show_data.get("last_air_date"),
            tmdb_data={
                "genres": [g["name"] for g in show_data.get("genres", [])],
                "external_ids": show_data.get("external_ids", {}),
                "seasons": [
                    {
                        "season_number": s["season_number"],
                        "poster_path": tmdb.poster_url(s.get("poster_path")),
                        "episode_count": s["episode_count"],
                        "name": s["name"],
                    }
                    for s in show_data.get("seasons", [])
                ],
            },
        )
        db.add(show)
        await db.flush()
    return show


# ── Shared helpers ─────────────────────────────────────────────────────────────

# Best-effort guard against a webhook delivery being processed twice in quick
# succession — media servers (and any relay/proxy in front of them) retry
# deliveries that don't get a fast 2xx, and nothing upstream de-duplicates
# those retries for us. This only catches immediate repeats within the same
# process; it's not a substitute for a persistent idempotency key, but it's
# low-risk and stops a retried `media.stop`/`media.scrobble` from re-firing
# the outbound Trakt/Simkl/MDBList scrobble calls.
_recent_webhook_deliveries: dict[str, datetime] = {}
_WEBHOOK_DEDUP_WINDOW = timedelta(seconds=15)


def _is_duplicate_webhook_delivery(dedup_key: str) -> bool:
    now = datetime.utcnow()
    if len(_recent_webhook_deliveries) > 2000:
        cutoff = now - _WEBHOOK_DEDUP_WINDOW
        for key, seen_at in list(_recent_webhook_deliveries.items()):
            if seen_at < cutoff:
                del _recent_webhook_deliveries[key]
    last_seen = _recent_webhook_deliveries.get(dedup_key)
    _recent_webhook_deliveries[dedup_key] = now
    return last_seen is not None and (now - last_seen) < _WEBHOOK_DEDUP_WINDOW


async def _get_or_open_session(
    db: AsyncSession,
    session_key: str,
    source: str,
    user_id: int,
    media_id: int,
) -> PlaybackSession:
    result = await db.execute(
        select(PlaybackSession).where(PlaybackSession.session_key == session_key)
    )
    session = result.scalar_one_or_none()
    if not session:
        session = PlaybackSession(
            session_key=session_key,
            source=source,
            user_id=user_id,
            media_id=media_id,
            progress_percent=0.0,
            progress_seconds=0,
            started_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(session)
        await db.flush()
    return session


async def _close_session(db: AsyncSession, session_key: str) -> Optional[PlaybackSession]:
    result = await db.execute(
        select(PlaybackSession).where(PlaybackSession.session_key == session_key)
    )
    session = result.scalar_one_or_none()
    if session:
        await db.delete(session)
    return session


async def _commit_playback_session_update(db: AsyncSession) -> bool:
    """Commits a pending PlaybackSession update, tolerating a concurrent
    PlaybackStop having already deleted that same row. Jellyfin/Emby send no
    dedup protection on webhook deliveries (unlike Plex), so an
    overlapping/duplicate progress tick can race a stop event for the same
    session_key and try to UPDATE a row that's already gone - SQLAlchemy
    surfaces that as a StaleDataError (0 rows matched) instead of a silent
    no-op, which otherwise crashes the whole request with a 500. Returns
    False (after rolling back) if that happened, True on a normal commit."""
    try:
        await db.commit()
        return True
    except StaleDataError:
        await db.rollback()
        return False


def _episode_for_progress(
    media_list: list["Media"], progress_percent: float, progress_seconds: int
) -> tuple["Media", float, int]:
    """Picks which episode of a multi-episode file (see #138) to treat as
    'currently playing', dividing the combined file's runtime evenly across
    its N episodes - e.g. episode 1 for the first third, episode 2 for the
    next third, episode 3 for the rest. Returns that episode plus its
    progress re-normalized to that episode's own segment (0..1, seconds) so
    the now-playing bar shows a coherent 0->100% per episode instead of
    jumping straight to ~33%/66% then resetting when the episode changes."""
    n = len(media_list)
    pct = progress_percent or 0.0
    idx = min(n - 1, max(0, int(pct * n)))
    segment_pct = max(0.0, min(1.0, pct * n - idx))
    segment_seconds = int(segment_pct * (progress_seconds / pct) / n) if pct > 0 else 0
    return media_list[idx], segment_pct, segment_seconds


async def _update_playback_progress(
    db: AsyncSession,
    user_id: int,
    media_id: int,
    progress_percent: float,
    progress_seconds: int,
) -> None:
    """Updates persistent in-progress state (Continue Watching)."""
    # Don't track progress below 5% or above 90% (those are handled by scrobble)
    if progress_percent < 0.05 or progress_percent >= 0.90:
        # If we already have progress and it's now outside the range, delete it
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == user_id,
                PlaybackProgress.media_id == media_id
            )
        )
        return

    result = await db.execute(
        select(PlaybackProgress).where(
            PlaybackProgress.user_id == user_id,
            PlaybackProgress.media_id == media_id
        )
    )
    progress = result.scalar_one_or_none()
    if progress:
        progress.progress_percent = progress_percent
        progress.progress_seconds = progress_seconds
        progress.updated_at = datetime.utcnow()
    else:
        db.add(PlaybackProgress(
            user_id=user_id,
            media_id=media_id,
            progress_percent=progress_percent,
            progress_seconds=progress_seconds,
            updated_at=datetime.utcnow(),
        ))


async def _write_watch_event(
    db: AsyncSession,
    user_id: int,
    media_id: int,
    progress_percent: float,
    progress_seconds: int,
    completed: bool,
) -> None:
    if completed:
        # A single completed viewing is often reported by more than one webhook
        # event for the same session (e.g. Plex sends both `media.scrobble` at
        # ~90% and `media.stop` when the session actually closes) — without this
        # guard each one adds its own WatchEvent row.
        recent_cutoff = datetime.utcnow() - timedelta(minutes=5)
        existing = await db.execute(
            select(WatchEvent.id).where(
                WatchEvent.user_id == user_id,
                WatchEvent.media_id == media_id,
                # NULL >= cutoff is false in SQL, so an unknown-dated event
                # (manually logged without a date) needs an explicit OR here
                # to still be recognized by this guard.
                or_(WatchEvent.watched_at.is_(None), WatchEvent.watched_at >= recent_cutoff),
            ).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return
        event = WatchEvent(
            user_id=user_id,
            media_id=media_id,
            watched_at=datetime.utcnow(),
            progress_seconds=progress_seconds,
            progress_percent=1.0,
            completed=True,
            play_count=1,
            # watched_at is this server's receipt time, not the media server's own
            # record of the play — provisional until an authoritative sync (e.g.
            # Plex's history backfill) confirms/corrects it. See GitHub #135.
            provisional=True,
        )
        db.add(event)
        # Remove any in-progress marker since it's now done
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == user_id,
                PlaybackProgress.media_id == media_id
            )
        )
        await db.flush()
        await record_rewatch_progress(db, user_id, media_id, event.id)
    else:
        # Just update in-progress state, don't add to WatchEvent (History)
        await _update_playback_progress(db, user_id, media_id, progress_percent, progress_seconds)


async def _handle_unwatch_toggle(db: AsyncSession, user_id: int, media: Media) -> bool:
    """Server-reported "mark unwatched" for one item (currently only Jellyfin's
    webhook plugin reports this - see the UserDataSaved/TogglePlayed handling
    below). While a rewatch is active for the media's show, this only undoes
    that episode's progress on the current cycle - real watch history is left
    untouched either way. Without an active rewatch, it removes all watch
    history for the item, matching this connection's normal bidirectional
    watched-status sync.

    Returns whether any row was actually deleted - callers use this to skip
    re-pushing the "unwatched" state out when this was a no-op (already
    unwatched). Without it, two two-way-sync connections can ping-pong: A's
    webhook pushes unwatched to B (excluding A), B's own webhook fires back
    reporting the same already-applied unwatch, and - since that inbound
    connection is B, not A - a naive re-push would go back out to A too,
    forever (see #190).
    """
    active_rewatch = None
    if media.media_type == MediaType.episode and media.show_id:
        active_rewatch = await get_active_rewatch(db, user_id, media.show_id)
    if active_rewatch:
        result = await db.execute(
            delete(RewatchProgress).where(
                RewatchProgress.rewatch_id == active_rewatch.id,
                RewatchProgress.media_id == media.id,
            )
        )
    else:
        result = await db.execute(
            delete(WatchEvent).where(
                WatchEvent.user_id == user_id,
                WatchEvent.media_id == media.id,
            )
        )
    return bool(result.rowcount)


# ── Jellyfin ───────────────────────────────────────────────────────────────────

def parse_jellyfin_payload(payload: dict) -> dict | None:
    # Emby doesn't send NotificationType at all - its webhooks report the event
    # under "Event" (dotted, lowercase names like "playback.stop"), which the
    # handlers below already know how to match - it just wasn't being read (#160).
    notification_type = (
        payload.get("NotificationType")
        or payload.get("notificationType")
        or payload.get("Event")
        or payload.get("event", "")
    )

    # ── Nested format (raw Jellyfin API / custom HTTP destination) ────────────
    item = payload.get("Item") or payload.get("item") or {}
    session = payload.get("Session") or payload.get("session") or {}
    if item and item.get("Type") in ("Movie", "Episode"):
        play_state = session.get("PlayState", {})
        # Emby resets Session.PlayState to the next (auto-playing) episode
        # before firing the "playback.stop" event for the one that just
        # finished, so PositionTicks/RunTimeTicks there can already read 0 -
        # PlaybackInfo carries this event's own, authoritative position and
        # completion state instead (see #206).
        playback_info = payload.get("PlaybackInfo") or {}
        position_ticks = play_state.get("PositionTicks") or playback_info.get("PositionTicks", 0)
        runtime_ticks = item.get("RunTimeTicks", 0)

        media_sources = item.get("MediaSources", [])
        if media_sources:
            streams = media_sources[0].get("MediaStreams", [])
            quality = extract_quality(streams)
            quality["file_path"] = media_sources[0].get("Path")
        else:
            quality = {}

        return {
            "notification_type": notification_type,
            "jellyfin_id": item.get("Id"),
            "title": item.get("Name"),
            "year": item.get("ProductionYear"),
            "media_type": "movie" if item.get("Type") == "Movie" else "episode",
            "tmdb_id": item.get("ProviderIds", {}).get("Tmdb"),
            "series_tmdb_id": item.get("SeriesProviderIds", {}).get("Tmdb"),
            # Emby's native webhook notifications use this nested shape and don't
            # reliably populate SeriesProviderIds the way Jellyfin's "send all
            # properties" plugin does - without a series_name fallback here,
            # find_or_create_media_jellyfin can never resolve show linkage for
            # an Emby episode, leaving Now Playing showing the episode title
            # with no poster instead of the series (see #192).
            "series_name": item.get("SeriesName"),
            "season_number": item.get("ParentIndexNumber"),
            "episode_number": item.get("IndexNumber"),
            # Jellyfin/Emby can mux several episodes into one file and fire a single
            # webhook event for it (see #138) - IndexNumberEnd marks the span.
            "episode_number_end": item.get("IndexNumberEnd"),
            "progress_percent": round(position_ticks / runtime_ticks, 4) if runtime_ticks else 0.0,
            "progress_seconds": int(position_ticks / 10_000_000) if position_ticks else 0,
            "is_paused": bool(play_state.get("IsPaused", False)),
            "session_id": session.get("Id") or session.get("PlaySessionId"),
            "username": session.get("UserName") or payload.get("NotificationUsername", ""),
            "quality": quality,
            # Authoritative "finished the item" signal for a stop event - trusted
            # over the computed position ratio above, which the auto-play race
            # above can zero out even though playback genuinely completed (#206).
            "played_to_completion": bool(playback_info.get("PlayedToCompletion")),
        }

    # ── Flat format (Jellyfin Webhook plugin — Generic Destination) ───────────
    item_type = payload.get("ItemType", "")
    if item_type not in ("Movie", "Episode"):
        return None

    tmdb_id = (
        payload.get("Provider_tmdb")
        or payload.get("Provider_Tmdb")
        or payload.get("Provider_tmdbid")
    )
    position_ticks = payload.get("PlaybackPositionTicks") or payload.get("PositionTicks") or 0
    runtime_ticks = payload.get("RunTimeTicks") or 0

    # SeasonNumber/EpisodeNumber are absent from the payload for movies;
    # 0 is a valid season number (specials), so don't coerce it away.
    season_num = payload.get("SeasonNumber")
    episode_num = payload.get("EpisodeNumber")

    return {
        "notification_type": notification_type,
        "jellyfin_id": payload.get("ItemId"),
        "title": payload.get("Name"),
        "year": payload.get("Year") or payload.get("ProductionYear"),
        "media_type": "movie" if item_type == "Movie" else "episode",
        "tmdb_id": str(tmdb_id) if tmdb_id else None,
        "series_tmdb_id": None,  # not exposed in flat format; resolved in find_or_create
        "series_name": payload.get("SeriesName"),  # used to look up show when series_tmdb_id is absent
        "season_number": season_num,
        "episode_number": episode_num,
        # "Send all properties" (the setup this repo documents, since custom
        # templates produce invalid JSON - see README) includes this alongside
        # EpisodeNumber for a multi-episode file (see #138 follow-up).
        "episode_number_end": payload.get("EpisodeNumberEnd"),
        "progress_percent": round(position_ticks / runtime_ticks, 4) if runtime_ticks else 0.0,
        "progress_seconds": int(position_ticks / 10_000_000) if position_ticks else 0,
        "is_paused": bool(payload.get("IsPaused", False)),
        "session_id": payload.get("PlaySessionId") or payload.get("DeviceId"),
        "username": payload.get("UserName") or payload.get("NotificationUsername", ""),
        "quality": {},
        # Only present on UserDataSaved events (manual watched/unwatched toggle,
        # rating change, favorite, etc. all raise this same notification type).
        "save_reason": payload.get("SaveReason"),
        "played": payload.get("Played"),
        # Same authoritative completion signal as the nested format's
        # PlaybackInfo.PlayedToCompletion (#206) - the flat plugin template
        # exposes it as its own top-level property.
        "played_to_completion": bool(payload.get("PlayedToCompletion")),
    }


async def _resolve_tvdb_fallback(
    db: AsyncSession, show: Show | None, user_id: int | None
) -> tuple[int | None, str | None, str | None]:
    """(tvdb_id, tvdb_api_key, tvdb_lang) for enrich_media's TVDB fallback -
    only worth a DB round-trip when the show actually has a TVDB match to
    fall back to (#162, #186).

    Used from webhook processing, which - same as enrich_media itself - must
    never fail the whole request over an enrichment nicety: a lookup failure
    here just means no TVDB fallback is attempted, same as if this feature
    didn't exist, not a crashed webhook.
    """
    if not (user_id and show and show.tvdb_id):
        return None, None, None
    try:
        from routers.shows import get_user_tvdb_key

        tvdb_api_key = await get_user_tvdb_key(db, user_id)
        if not tvdb_api_key:
            return show.tvdb_id, None, None
        tvdb_lang = tvdb_client.tvdb_language(await get_user_metadata_language(db, user_id))
        return show.tvdb_id, tvdb_api_key, tvdb_lang
    except Exception:
        return None, None, None


async def _resolve_show_for_episode(
    data: dict, db: AsyncSession, api_key: str = None
) -> tuple[Show | None, int | None]:
    """(show, series_tmdb_id) for a parsed Jellyfin/Emby webhook payload.
    Falls back to a series_name lookup (local Show table, then TMDB search)
    when the payload carries no series_tmdb_id - the flat plugin format never
    has one, and Emby's nested webhooks omit it too (#192)."""
    show = None
    series_tmdb_id = int(data["series_tmdb_id"]) if data.get("series_tmdb_id") else None

    if data["media_type"] == "episode" and not series_tmdb_id and data.get("series_name"):
        # Flat format: no series_tmdb_id — try local Show table first, then TMDB search
        local_result = await db.execute(
            select(Show).where(Show.title.ilike(data["series_name"]))
        )
        local_show = local_result.scalars().first()
        if local_show:
            series_tmdb_id = local_show.tmdb_id
        else:
            try:
                res = await tmdb.search_shows(data["series_name"], api_key=api_key)
                if res.get("results"):
                    series_tmdb_id = res["results"][0]["id"]
            except Exception:
                pass

    if data["media_type"] == "episode" and series_tmdb_id:
        try:
            show = await _find_or_create_show(db, series_tmdb_id, api_key)
        except Exception:
            pass

    return show, series_tmdb_id


async def find_or_create_media_jellyfin(
    data: dict, db: AsyncSession, api_key: str = None, user_id: int | None = None
) -> Media | None:
    # 1. Match by source item ID via CollectionFile (fastest path post-sync).
    # This function is shared by both Jellyfin and Emby webhooks (they're the
    # same REST API) - matching only CollectionSource.jellyfin meant every
    # Emby-sourced item always missed this fast path and fell through to the
    # slower show/tmdb_id resolution below, even for episodes already synced
    # and correctly linked (contributing to #192).
    # A multi-episode file (see #138) has several CollectionFiles sharing this
    # source_id, one per episode - disambiguate by episode_number whenever the
    # caller knows which one it wants (find_or_create_media_jellyfin_multi sets
    # it per sub-call), so this doesn't just grab an arbitrary sibling episode.
    if data["jellyfin_id"]:
        query = (
            select(Media)
            .join(Collection, Collection.media_id == Media.id)
            .join(CollectionFile, CollectionFile.collection_id == Collection.id)
            .where(CollectionFile.source.in_((CollectionSource.jellyfin, CollectionSource.emby)))
            .where(CollectionFile.source_id == data["jellyfin_id"])
        )
        if data["media_type"] == "episode" and data.get("episode_number") is not None:
            query = query.where(Media.episode_number == data["episode_number"])
        result = await db.execute(query)
        media = result.scalars().first()
        if media:
            # This row may predate the series_name/CollectionSource.emby fixes
            # above (or the show lookup simply failed at creation time) and
            # still be missing show linkage - without this, Now Playing keeps
            # showing the bare episode title forever for that item even after
            # upgrading, since this fast path would otherwise return it as-is
            # on every future webhook too (#192 follow-up).
            if media.media_type == MediaType.episode and media.show_id is None:
                show, series_tmdb_id = await _resolve_show_for_episode(data, db, api_key)
                if show:
                    media.show_id = show.id
                    tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, user_id)
                    await enrich_media(
                        media, api_key=api_key, series_tmdb_id=series_tmdb_id,
                        tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
                    )
            return media

    # Resolve show for episode dedup and enrichment
    show, series_tmdb_id = await _resolve_show_for_episode(data, db, api_key)

    # 2. Match by TMDB ID (handles rapid webhook events before first sync, or items
    #    already added via another source / manually — prevents duplicate media rows)
    if data["tmdb_id"]:
        result = await db.execute(
            select(Media).where(
                Media.tmdb_id == int(data["tmdb_id"]),
                Media.media_type == MediaType(data["media_type"]),
            )
        )
        media = result.scalars().first()
        if media:
            if media.media_type == MediaType.episode and media.show_id is None and show:
                media.show_id = show.id
                tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, user_id)
                await enrich_media(
                    media, api_key=api_key, series_tmdb_id=series_tmdb_id,
                    tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
                )
            return media

    # 2b. Movie matching by title + year if TMDB ID is missing
    if data["media_type"] == "movie" and not data["tmdb_id"]:
        # Try local match first to avoid redundant TMDB search
        local_q = select(Media).where(
            Media.media_type == MediaType.movie,
            Media.title.ilike(data["title"]),
        )
        if data.get("year"):
            local_q = local_q.where(Media.release_date.like(f"{data['year']}%"))
        
        media = (await db.execute(local_q)).scalars().first()
        if media:
            return media
            
        # Try TMDB search to find the real ID
        try:
            search_res = await tmdb.search_movies(data["title"], year=data.get("year"), api_key=api_key)
            if search_res.get("results"):
                tmdb_movie = search_res["results"][0]
                data["tmdb_id"] = str(tmdb_movie["id"])
                # Check again with the new TMDB ID
                result = await db.execute(
                    select(Media).where(
                        Media.tmdb_id == tmdb_movie["id"],
                        Media.media_type == MediaType.movie,
                    )
                )
                media = result.scalars().first()
                if media:
                    return media
        except Exception:
            pass

    # 3. Match by (show_id, season_number, episode_number) — catches sync-created rows
    #    when the Jellyfin item's TMDB ID is missing or doesn't match
    if show and data["season_number"] is not None and data["episode_number"] is not None:
        result = await db.execute(
            select(Media).where(
                Media.media_type == MediaType.episode,
                Media.show_id == show.id,
                Media.season_number == data["season_number"],
                Media.episode_number == data["episode_number"],
            )
        )
        media = result.scalars().first()
        if media:
            return media

    # Don't create a row for an episode we can't identify at all — it can never
    # be enriched or matched back to a real episode, and would inflate collection counts.
    if data["media_type"] == "episode" and data["season_number"] is None and data["episode_number"] is None and not data["tmdb_id"]:
        print(f"  Skipping unidentifiable episode '{data['title']}' (no season/episode/tmdb_id)")
        return None

    media, _created = await create_media_safely(
        db,
        int(data["tmdb_id"]) if data["tmdb_id"] else None,
        MediaType(data["media_type"]),
        title=data["title"],
        season_number=data["season_number"],
        episode_number=data["episode_number"],
        show_id=show.id if show else None,
    )
    if show and series_tmdb_id:
        tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, user_id)
        media = await enrich_media_safely(
            db, media, api_key=api_key, series_tmdb_id=series_tmdb_id,
            tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
        )
    else:
        await enrich_media(media, api_key=api_key)
    return media


async def find_or_create_media_jellyfin_multi(
    data: dict, db: AsyncSession, api_key: str = None, user_id: int | None = None
) -> list[Media]:
    """Resolves every episode a Jellyfin/Emby webhook event covers - almost
    always just one, but a multi-episode file (IndexNumber..IndexNumberEnd,
    see #138) fires a single webhook event for the whole combined file.
    Expands into one find_or_create_media_jellyfin() call per episode number
    in the span so scrobbling/marking-watched applies to every episode, not
    just the first (the gap bittom reported in #138)."""
    start = data.get("episode_number")
    end = data.get("episode_number_end")
    if data["media_type"] != "episode" or start is None or end is None or end <= start:
        media = await find_or_create_media_jellyfin(data, db, api_key=api_key, user_id=user_id)
        return [media] if media else []

    results: list[Media] = []
    for ep in range(start, end + 1):
        media = await find_or_create_media_jellyfin(dict(data, episode_number=ep), db, api_key=api_key, user_id=user_id)
        if media:
            results.append(media)
    return results


async def _handle_jellyfin_webhook(request: Request, db: AsyncSession, api_key: str, connection_id: int | None = None):
    user_result = await db.execute(select(User).where(User.api_key == api_key))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.body()
    if not body:
        return {"status": "ignored", "reason": "empty body"}

    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "invalid JSON"}

    data = parse_jellyfin_payload(payload)
    if not data:
        return {"status": "ignored"}

    notification_type = data["notification_type"]

    if connection_id is not None:
        conn = await _get_connection_by_id(db, user.id, connection_id)
    else:
        conn = await _get_oldest_connection(db, user.id, "jellyfin")

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    tmdb_key = await _get_tmdb_key(db, settings)

    # Almost always one episode; a combined multi-episode file (see #138)
    # resolves to several. "Now playing"/live progress scrobbles below stay
    # keyed on media (the first episode) — a single stream position doesn't
    # map onto per-sub-episode progress. Collection and completed-watch
    # events loop media_list so every episode in the file is covered.
    media_list = await find_or_create_media_jellyfin_multi(data, db, api_key=tmdb_key, user_id=user.id)
    session_key = f"jellyfin:{user.id}:{data['session_id']}"

    if not media_list:
        return {"status": "ignored", "reason": "episode could not be identified (no season/episode/tmdb_id)"}
    media = media_list[0]

    # ItemAdded fires once, right when a title lands in the library — often
    # long before anyone plays it, so "add to collection" can't wait on a
    # playback event to piggy-back on (see #129: an added-but-unwatched item
    # never showed up in Scrob until it was played or a full sync ran).
    if notification_type in ("PlaybackStart", "PlaybackProgress", "PlaybackStop", "MarkPlayed", "ItemAdded", "playback.start", "playback.progress", "playback.stop", "item.markplayed"):
        if not conn or conn.sync_collection:
            allow_collection = True
            jellyfin_id = data.get("jellyfin_id")
            if jellyfin_id and conn:
                sel_result = await db.execute(
                    select(JellyfinLibrarySelection).where(JellyfinLibrarySelection.connection_id == conn.id)
                )
                selected_ids = {row.library_id for row in sel_result.scalars().all()}
                if selected_ids:
                    import core.jellyfin as jellyfin_client
                    # user_id is required here - Jellyfin's admin-only Items/{id}
                    # endpoint (no Users/ prefix) throws server-side for a
                    # non-admin token (see #179).
                    item_data = await jellyfin_client.get_item(conn.url, conn.token, jellyfin_id, user_id=conn.server_user_id)
                    library_id: str | None = None
                    if item_data:
                        if item_data.get("Type") == "Episode":
                            series_id = item_data.get("SeriesId")
                            if series_id:
                                series_data = await jellyfin_client.get_item(conn.url, conn.token, series_id, user_id=conn.server_user_id)
                                library_id = (series_data or {}).get("ParentId")
                        else:
                            library_id = item_data.get("ParentId")
                    allow_collection = library_id in selected_ids if library_id else True

            if allow_collection:
                for m in media_list:
                    await _ensure_collection_entry(
                        db, user.id, m.id, CollectionSource.jellyfin, data["jellyfin_id"], data.get("quality"),
                        connection_id=conn.id if conn else None,
                    )
                # Needed here specifically for ItemAdded: unlike the playback
                # notification types, nothing later in this request commits —
                # without this, the insert is silently rolled back when the
                # request's session closes (see #129 followup).
                await db.commit()

    # See #129 — the counterpart to ItemAdded above: a title removed from the
    # Jellyfin library should leave the user's collection too, rather than
    # only being cleaned up on the next full sync.
    elif notification_type == "ItemDeleted":
        if (not conn or conn.sync_collection) and data.get("jellyfin_id"):
            for m in media_list:
                await _remove_collection_entry(
                    db, user.id, m.id, CollectionSource.jellyfin, data["jellyfin_id"],
                )
            await db.commit()

    if notification_type in ("PlaybackStart", "playback.start"):
        if not conn or conn.sync_playback:
            # Multi-episode file: show whichever episode the file-wide progress
            # currently falls into (see #138 follow-up), not always the first.
            current_episode, _, _ = _episode_for_progress(media_list, data["progress_percent"], data["progress_seconds"])
            session = await _get_or_open_session(db, session_key, "jellyfin", user.id, current_episode.id)
            session.media_id = current_episode.id
            session.state = "playing"
            await _commit_playback_session_update(db)
        await _maybe_trakt_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_mdblist_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_simkl_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_bingebase_scrobble(settings, media, "start", data["progress_percent"], db=db)

    elif notification_type in ("PlaybackProgress", "playback.progress"):
        if not conn or conn.sync_playback:
            current_episode, segment_pct, segment_seconds = _episode_for_progress(
                media_list, data["progress_percent"], data["progress_seconds"]
            )
            session = await _get_or_open_session(db, session_key, "jellyfin", user.id, current_episode.id)
            session.media_id = current_episode.id
            session.state = "paused" if data["is_paused"] else "playing"
            session.progress_percent = segment_pct
            session.progress_seconds = segment_seconds
            session.updated_at = datetime.utcnow()
            await _commit_playback_session_update(db)
        if data["is_paused"]:
            await _maybe_trakt_scrobble(settings, media, "pause", data["progress_percent"], db=db)
            await _maybe_mdblist_scrobble(settings, media, "pause", data["progress_percent"], db=db)
            await _maybe_bingebase_scrobble(settings, media, "pause", data["progress_percent"], db=db)

    elif notification_type in ("PlaybackStop", "playback.stop"):
        # sync_watched and sync_playback are independent toggles - watched status
        # must sync even when continue-watching tracking is off, and _close_session's
        # pending delete needs committing either way (was only ever reached when
        # sync_playback was on, leaving the closed session uncommitted otherwise).
        session = await _close_session(db, session_key)
        progress_percent = data["progress_percent"] or (session.progress_percent if session else 0.0)
        progress_seconds = data["progress_seconds"] or (session.progress_seconds if session else 0)
        if data.get("played_to_completion"):
            # Trust this over the computed ratio above - an Emby auto-play
            # transition can zero out position/runtime for the item that just
            # finished before this stop event is built, silently dropping a
            # genuine completion under the 5% floor below otherwise (#206).
            progress_percent = 1.0
        if (not conn or conn.sync_watched) and progress_percent > 0.05:
            for m in media_list:
                await _write_watch_event(db, user.id, m.id, progress_percent, progress_seconds, progress_percent >= 0.90)
        await db.commit()
        for m in media_list:
            await _maybe_trakt_scrobble(settings, m, "stop", progress_percent, db=db)
            await _maybe_mdblist_scrobble(settings, m, "stop", progress_percent, db=db)
            await _maybe_simkl_scrobble(settings, m, "stop", progress_percent, db=db)
            await _maybe_bingebase_scrobble(settings, m, "stop", progress_percent, db=db)

    elif notification_type in ("MarkPlayed", "item.markplayed"):
        # Same reasoning as PlaybackStop above: _close_session's pending delete
        # needs committing regardless of sync_watched, not only when it fires.
        await _close_session(db, session_key)
        if not conn or conn.sync_watched:
            for m in media_list:
                await _write_watch_event(db, user.id, m.id, 1.0, data["progress_seconds"], True)
        await db.commit()
        for m in media_list:
            await _maybe_trakt_scrobble(settings, m, "stop", 1.0, db=db)
            await _maybe_mdblist_scrobble(settings, m, "stop", 1.0, db=db)
            await _maybe_simkl_scrobble(settings, m, "stop", 1.0, db=db)
            await _maybe_bingebase_scrobble(settings, m, "stop", 1.0, db=db)

    elif notification_type == "UserDataSaved":
        # Jellyfin's official Webhook plugin has no dedicated "mark played"
        # event — manually toggling watched/unwatched (and rating changes,
        # favorites, imports, and every playback tick) all raise this same
        # UserDataSaved notification. SaveReason is the only way to tell a
        # manual watched-state toggle apart from the rest.
        if data.get("save_reason") == "TogglePlayed" and (not conn or conn.sync_watched):
            played = data.get("played")
            if played:
                await _close_session(db, session_key)
                for m in media_list:
                    await _write_watch_event(db, user.id, m.id, 1.0, data["progress_seconds"], True)
                await db.commit()
                for m in media_list:
                    await _maybe_trakt_scrobble(settings, m, "stop", 1.0, db=db)
                    await _maybe_mdblist_scrobble(settings, m, "stop", 1.0, db=db)
                    await _maybe_simkl_scrobble(settings, m, "stop", 1.0, db=db)
                    await _maybe_bingebase_scrobble(settings, m, "stop", 1.0, db=db)
            elif played is False:
                changed_ids = [
                    m.id for m in media_list
                    if await _handle_unwatch_toggle(db, user.id, m)
                ]
                await db.commit()
                if changed_ids:
                    from routers.history import _push_watch_state
                    # exclude_connection_id: this unwatch was itself reported BY this
                    # connection - pushing it right back to the same server is what
                    # causes the infinite webhook loop in #190. Still propagates to
                    # any OTHER connection with push_watched enabled. changed_ids
                    # (rather than every m in media_list) additionally skips the
                    # push entirely when nothing was actually deleted - closing the
                    # multi-connection ping-pong case exclude_connection_id alone
                    # doesn't cover (see _handle_unwatch_toggle's docstring).
                    await _push_watch_state(
                        db, user.id, changed_ids, watched=False,
                        exclude_connection_id=conn.id if conn else None,
                    )

    return {"status": "ok", "event": notification_type, "title": data["title"]}


@router.post("/jellyfin")
async def jellyfin_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_jellyfin_webhook(request, db, api_key)


@router.post("/jellyfin/scrobble/{connection_id}")
async def jellyfin_scrobble_webhook(
    connection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_jellyfin_scrobble_webhook(request, db, api_key, connection_id, source="jellyfin")


@router.post("/jellyfin/{connection_id}")
async def jellyfin_webhook_connection(
    connection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_jellyfin_webhook(request, db, api_key, connection_id)


# ── Emby ───────────────────────────────────────────────────────────────────────

async def _handle_emby_webhook(request: Request, db: AsyncSession, api_key: str, connection_id: int | None = None):
    user_result = await db.execute(select(User).where(User.api_key == api_key))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.body()
    if not body:
        return {"status": "ignored", "reason": "empty body"}

    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "invalid JSON"}

    data = parse_jellyfin_payload(payload)
    if not data:
        return {"status": "ignored"}

    notification_type = data["notification_type"]

    if connection_id is not None:
        conn = await _get_connection_by_id(db, user.id, connection_id)
    else:
        conn = await _get_oldest_connection(db, user.id, "emby")

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    tmdb_key = await _get_tmdb_key(db, settings)

    # See the matching comment in _handle_jellyfin_webhook (#138 follow-up).
    media_list = await find_or_create_media_jellyfin_multi(data, db, api_key=tmdb_key, user_id=user.id)
    session_key = f"emby:{user.id}:{data['session_id']}"

    if not media_list:
        return {"status": "ignored", "reason": "episode could not be identified (no season/episode/tmdb_id)"}
    media = media_list[0]

    # See the matching comment in _handle_jellyfin_webhook (#129).
    if notification_type in ("PlaybackStart", "PlaybackProgress", "PlaybackStop", "MarkPlayed", "ItemAdded", "playback.start", "playback.progress", "playback.stop", "item.markplayed"):
        if not conn or conn.sync_collection:
            allow_collection = True
            emby_item_id = data.get("jellyfin_id")
            if emby_item_id and conn:
                sel_result = await db.execute(
                    select(EmbyLibrarySelection).where(EmbyLibrarySelection.connection_id == conn.id)
                )
                selected_ids = {row.library_id for row in sel_result.scalars().all()}
                if selected_ids:
                    import core.emby as emby_client
                    # user_id is required here - same reasoning as the Jellyfin
                    # branch above (see #179).
                    item_data = await emby_client.get_item(conn.url, conn.token, emby_item_id, user_id=conn.server_user_id)
                    library_id: str | None = None
                    if item_data:
                        if item_data.get("Type") == "Episode":
                            series_id = item_data.get("SeriesId")
                            if series_id:
                                series_data = await emby_client.get_item(conn.url, conn.token, series_id, user_id=conn.server_user_id)
                                library_id = (series_data or {}).get("ParentId")
                        else:
                            library_id = item_data.get("ParentId")
                    allow_collection = library_id in selected_ids if library_id else True

            if allow_collection:
                for m in media_list:
                    await _ensure_collection_entry(
                        db, user.id, m.id, CollectionSource.emby, data["jellyfin_id"], data.get("quality"),
                        connection_id=conn.id if conn else None,
                    )
                # See the matching comment in _handle_jellyfin_webhook (#129).
                await db.commit()

    # See the matching comment in _handle_jellyfin_webhook (#129).
    elif notification_type == "ItemDeleted":
        if (not conn or conn.sync_collection) and data.get("jellyfin_id"):
            for m in media_list:
                await _remove_collection_entry(
                    db, user.id, m.id, CollectionSource.emby, data["jellyfin_id"],
                )
            await db.commit()

    if notification_type in ("PlaybackStart", "playback.start"):
        if not conn or conn.sync_playback:
            session = await _get_or_open_session(db, session_key, "emby", user.id, media.id)
            session.state = "playing"
            await _commit_playback_session_update(db)
        await _maybe_trakt_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_mdblist_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_simkl_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_bingebase_scrobble(settings, media, "start", data["progress_percent"], db=db)

    elif notification_type in ("PlaybackProgress", "playback.progress"):
        if not conn or conn.sync_playback:
            session = await _get_or_open_session(db, session_key, "emby", user.id, media.id)
            session.state = "paused" if data["is_paused"] else "playing"
            session.progress_percent = data["progress_percent"]
            session.progress_seconds = data["progress_seconds"]
            session.updated_at = datetime.utcnow()
            await _commit_playback_session_update(db)
        if data["is_paused"]:
            await _maybe_trakt_scrobble(settings, media, "pause", data["progress_percent"], db=db)
            await _maybe_mdblist_scrobble(settings, media, "pause", data["progress_percent"], db=db)
            await _maybe_bingebase_scrobble(settings, media, "pause", data["progress_percent"], db=db)

    elif notification_type in ("PlaybackStop", "playback.stop"):
        # sync_watched and sync_playback are independent toggles - watched status
        # must sync even when continue-watching tracking is off, and _close_session's
        # pending delete needs committing either way (was only ever reached when
        # sync_playback was on, leaving the closed session uncommitted otherwise).
        session = await _close_session(db, session_key)
        progress_percent = data["progress_percent"] or (session.progress_percent if session else 0.0)
        progress_seconds = data["progress_seconds"] or (session.progress_seconds if session else 0)
        if data.get("played_to_completion"):
            # See the matching comment in _handle_jellyfin_webhook (#206).
            progress_percent = 1.0
        if (not conn or conn.sync_watched) and progress_percent > 0.05:
            for m in media_list:
                await _write_watch_event(db, user.id, m.id, progress_percent, progress_seconds, progress_percent >= 0.90)
        await db.commit()
        for m in media_list:
            await _maybe_trakt_scrobble(settings, m, "stop", progress_percent, db=db)
            await _maybe_mdblist_scrobble(settings, m, "stop", progress_percent, db=db)
            await _maybe_simkl_scrobble(settings, m, "stop", progress_percent, db=db)
            await _maybe_bingebase_scrobble(settings, m, "stop", progress_percent, db=db)

    elif notification_type in ("MarkPlayed", "item.markplayed"):
        # Same reasoning as PlaybackStop above: _close_session's pending delete
        # needs committing regardless of sync_watched, not only when it fires.
        await _close_session(db, session_key)
        if not conn or conn.sync_watched:
            for m in media_list:
                await _write_watch_event(db, user.id, m.id, 1.0, data["progress_seconds"], True)
        await db.commit()
        for m in media_list:
            await _maybe_trakt_scrobble(settings, m, "stop", 1.0, db=db)
            await _maybe_mdblist_scrobble(settings, m, "stop", 1.0, db=db)
            await _maybe_simkl_scrobble(settings, m, "stop", 1.0, db=db)
            await _maybe_bingebase_scrobble(settings, m, "stop", 1.0, db=db)

    return {"status": "ok", "event": notification_type, "title": data["title"]}


async def _handle_jellyfin_scrobble_webhook(
    request: Request, db: AsyncSession, api_key: str, connection_id: int, source: str
):
    user_result = await db.execute(select(User).where(User.api_key == api_key))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.body()
    if not body:
        return {"status": "ignored", "reason": "empty body"}

    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "invalid JSON"}

    data = parse_jellyfin_payload(payload)
    if not data:
        return {"status": "ignored"}

    conn = await _get_scrobble_connection_by_id(db, user.id, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Scrobble connection not found")

    notification_type = data["notification_type"]

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    tmdb_key = await _get_tmdb_key(db, settings)

    # See the matching comment in _handle_jellyfin_webhook (#138 follow-up).
    media_list = await find_or_create_media_jellyfin_multi(data, db, api_key=tmdb_key, user_id=user.id)
    session_key = f"{source}:scrobble:{user.id}:{data['session_id']}"

    if not media_list:
        return {"status": "ignored", "reason": "episode could not be identified (no season/episode/tmdb_id)"}
    media = media_list[0]

    coll_source = CollectionSource.jellyfin if source == "jellyfin" else CollectionSource.emby

    # See the matching comment in _handle_jellyfin_webhook (#129).
    if notification_type in ("PlaybackStart", "PlaybackProgress", "PlaybackStop", "MarkPlayed", "ItemAdded", "playback.start", "playback.progress", "playback.stop", "item.markplayed"):
        if conn.sync_collection:
            for m in media_list:
                await _ensure_collection_entry(
                    db, user.id, m.id, coll_source, data["jellyfin_id"], data.get("quality"),
                    connection_id=None,
                )
            # See the matching comment in _handle_jellyfin_webhook (#129).
            await db.commit()

    elif notification_type == "ItemDeleted":
        if conn.sync_collection and data.get("jellyfin_id"):
            for m in media_list:
                await _remove_collection_entry(
                    db, user.id, m.id, coll_source, data["jellyfin_id"],
                )
            await db.commit()

    if notification_type in ("PlaybackStart", "playback.start"):
        if conn.sync_playback:
            session = await _get_or_open_session(db, session_key, source, user.id, media.id)
            session.state = "playing"
            await _commit_playback_session_update(db)

    elif notification_type in ("PlaybackProgress", "playback.progress"):
        if conn.sync_playback:
            session = await _get_or_open_session(db, session_key, source, user.id, media.id)
            session.state = "paused" if data["is_paused"] else "playing"
            session.progress_percent = data["progress_percent"]
            session.progress_seconds = data["progress_seconds"]
            session.updated_at = datetime.utcnow()
            await _commit_playback_session_update(db)

    elif notification_type in ("PlaybackStop", "playback.stop"):
        # sync_watched and sync_playback are independent toggles - watched status
        # must sync even when continue-watching tracking is off, and _close_session's
        # pending delete needs committing either way (was only ever reached when
        # sync_playback was on, leaving the closed session uncommitted otherwise).
        session = await _close_session(db, session_key)
        progress_percent = data["progress_percent"] or (session.progress_percent if session else 0.0)
        progress_seconds = data["progress_seconds"] or (session.progress_seconds if session else 0)
        if data.get("played_to_completion"):
            # See the matching comment in _handle_jellyfin_webhook (#206).
            progress_percent = 1.0
        if conn.sync_watched and progress_percent > 0.05:
            for m in media_list:
                await _write_watch_event(db, user.id, m.id, progress_percent, progress_seconds, progress_percent >= 0.90)
        await db.commit()

    elif notification_type in ("MarkPlayed", "item.markplayed"):
        # Same reasoning as PlaybackStop above: _close_session's pending delete
        # needs committing regardless of sync_watched, not only when it fires.
        await _close_session(db, session_key)
        if conn.sync_watched:
            for m in media_list:
                await _write_watch_event(db, user.id, m.id, 1.0, data["progress_seconds"], True)
        await db.commit()

    elif notification_type == "UserDataSaved":
        # Jellyfin's official Webhook plugin has no dedicated "mark played"
        # event - manually toggling watched/unwatched raises this same
        # UserDataSaved notification (see the matching comment in
        # _handle_jellyfin_webhook, #129). Scrobble-only connections never
        # handled this at all, so a manual toggle in Jellyfin/Emby's own UI
        # never propagated for them.
        if data.get("save_reason") == "TogglePlayed" and conn.sync_watched:
            played = data.get("played")
            if played:
                await _close_session(db, session_key)
                for m in media_list:
                    await _write_watch_event(db, user.id, m.id, 1.0, data["progress_seconds"], True)
                await db.commit()
            elif played is False:
                changed_ids = [
                    m.id for m in media_list
                    if await _handle_unwatch_toggle(db, user.id, m)
                ]
                await db.commit()
                if changed_ids:
                    from routers.history import _push_watch_state
                    # Unlike _handle_jellyfin_webhook, there's no exclude_connection_id
                    # here - a ScrobbleConnection has no url of its own, so it can't be
                    # matched against push-enabled MediaServerConnections to identify
                    # "the server this came from" (see #190). This only becomes a loop
                    # if the user also has a separate full MediaServerConnection with
                    # push_watched enabled pointing at that same physical server.
                    # changed_ids still helps here too: skips the push when this
                    # delivery was a no-op (already unwatched), same reasoning as
                    # _handle_jellyfin_webhook.
                    await _push_watch_state(db, user.id, changed_ids, watched=False)

    return {"status": "ok", "event": notification_type, "title": data["title"]}


@router.post("/emby")
async def emby_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_emby_webhook(request, db, api_key)


@router.post("/emby/scrobble/{connection_id}")
async def emby_scrobble_webhook(
    connection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_jellyfin_scrobble_webhook(request, db, api_key, connection_id, source="emby")


@router.post("/emby/{connection_id}")
async def emby_webhook_connection(
    connection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_emby_webhook(request, db, api_key, connection_id)


# ── Plex ───────────────────────────────────────────────────────────────────────

def parse_plex_payload(payload: dict) -> dict | None:
    event = payload.get("event", "")
    metadata = payload.get("Metadata") or {}
    media_type = metadata.get("type")  # "movie" | "episode"

    if media_type not in ("movie", "episode") and event not in ("library.new", "library.update"):
        return None

    # Skip live TV streams — no stable media identity, creates junk history/sessions
    if metadata.get("librarySectionType") == "livetv" or metadata.get("live"):
        return None

    # Extract TMDB ID from Guid array: [{"id": "tmdb://12345"}, ...]
    guids = metadata.get("Guid") or []
    tmdb_id: Optional[str] = None
    tvdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    for g in guids:
        gid = g.get("id", "")
        if gid.startswith("tmdb://"):
            tmdb_id = gid.replace("tmdb://", "")
        elif gid.startswith("tvdb://"):
            tvdb_id = gid.replace("tvdb://", "")
        elif gid.startswith("imdb://"):
            imdb_id = gid.replace("imdb://", "")

    # Extract series identifiers from grandparent
    grandparent_guid = metadata.get("grandparentGuid", "")
    grandparent_tmdb_id: Optional[str] = None
    grandparent_tvdb_id: Optional[str] = None
    grandparent_imdb_id: Optional[str] = None

    # Try regex on grandparentGuid — handle both modern short forms (tmdb://, tvdb://)
    # and legacy Plex agent forms (com.plexapp.agents.themoviedb://, thetvdb://)
    tmdb_match = re.search(r'(?:^tmdb|themoviedb(?:\.com)?)://(\d+)', grandparent_guid, re.IGNORECASE)
    if tmdb_match:
        grandparent_tmdb_id = tmdb_match.group(1)
    tvdb_match = re.search(r'(?:^tvdb|thetvdb(?:\.com)?)://(\d+)', grandparent_guid, re.IGNORECASE)
    if tvdb_match:
        grandparent_tvdb_id = tvdb_match.group(1)
    imdb_match = re.search(r'imdb://(tt\d+)', grandparent_guid, re.IGNORECASE)
    if imdb_match:
        grandparent_imdb_id = imdb_match.group(1)

    view_offset_ms = metadata.get("viewOffset", 0)
    duration_ms = metadata.get("duration", 0)
    progress_percent = round(view_offset_ms / duration_ms, 4) if duration_ms else 0.0
    progress_seconds = int(view_offset_ms / 1000)

    # Extract quality from the 'Media' list if present (common in library.new)
    media_list = metadata.get("Media", [])
    quality = {}
    if media_list:
        m = media_list[0]
        h = m.get("height", 0)
        w = m.get("width", 0)
        plex_res = str(m.get("videoResolution", "")).lower()
        if plex_res in ("4k", "2160"): resolution = "4K"
        elif plex_res == "1080": resolution = "1080p"
        elif plex_res == "720": resolution = "720p"
        elif plex_res in ("480", "sd"): resolution = "480p"
        elif plex_res: resolution = f"{plex_res}p"
        elif w >= 3200 or h >= 2000: resolution = "4K"
        elif w >= 1700 or h >= 800: resolution = "1080p"
        elif w >= 1100 or h >= 540: resolution = "720p"
        else: resolution = f"{h}p"

        quality = {
            "resolution": resolution,
            "video_codec": m.get("videoCodec"),
            "audio_codec": m.get("audioCodec"),
            "audio_channels": f"{m.get('audioChannels', 0)}.0" if m.get("audioChannels") else None,
            "audio_languages": [],
            "subtitle_languages": [],
        }
        parts = m.get("Part", [])
        if parts:
            p = parts[0]
            quality["file_path"] = p.get("file")
            for s in p.get("Stream", []):
                st = s.get("streamType")
                l = s.get("languageTag") or s.get("languageCode") or s.get("language")
                if not l: continue
                if st == 2 and l not in quality["audio_languages"]: quality["audio_languages"].append(l)
                elif st == 3 and l not in quality["subtitle_languages"]: quality["subtitle_languages"].append(l)

    return {
        "event": event,
        "title": metadata.get("title") or metadata.get("grandparentTitle", ""),
        "year": metadata.get("year"),
        "media_type": "movie" if media_type == "movie" else "episode",
        "tmdb_id": tmdb_id,
        "tvdb_id": tvdb_id,
        "imdb_id": imdb_id,
        "season_number": metadata.get("parentIndex"),
        "episode_number": metadata.get("index"),
        "rating": metadata.get("userRating"),
        "session_key": metadata.get("sessionKey") or metadata.get("ratingKey", ""),
        "progress_percent": progress_percent,
        "progress_seconds": progress_seconds,
        "duration_ms": duration_ms,
        "plex_rating_key": metadata.get("ratingKey"),
        "library_section_id": str(metadata["librarySectionID"]) if metadata.get("librarySectionID") else None,
        "library_section_type": metadata.get("librarySectionType"),
        "account_title": (payload.get("Account") or {}).get("title", ""),
        "grandparent_tmdb_id": grandparent_tmdb_id,
        "grandparent_tvdb_id": grandparent_tvdb_id,
        "grandparent_imdb_id": grandparent_imdb_id,
        "grandparent_title": metadata.get("grandparentTitle"),
        "grandparent_rating_key": str(metadata["grandparentRatingKey"]) if metadata.get("grandparentRatingKey") else None,
        "quality": quality,
    }


async def _ensure_collection_entry(
    db: AsyncSession,
    user_id: int,
    media_id: int,
    source: CollectionSource,
    source_id: str,
    quality: dict = None,
    connection_id: int | None = None,
) -> None:
    """Ensures a Collection + CollectionFile entry exists for the user, creating or updating as needed."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not quality:
        quality = {}

    # 1. Upsert the Collection row (one per user+media)
    coll_stmt = pg_insert(Collection).values(user_id=user_id, media_id=media_id)
    coll_stmt = coll_stmt.on_conflict_do_nothing(constraint="uq_collection_user_media")
    await db.execute(coll_stmt)
    await db.flush()

    # Fetch the canonical collection id
    coll_result = await db.execute(
        select(Collection.id).where(Collection.user_id == user_id, Collection.media_id == media_id)
    )
    collection_id = coll_result.scalar_one()

    # 2. Upsert the CollectionFile row (one per collection+source+source_id)
    update_dict: dict = {}
    if connection_id is not None:          update_dict["connection_id"]       = connection_id
    if quality.get("resolution"):         update_dict["resolution"]         = quality["resolution"]
    if quality.get("video_codec"):        update_dict["video_codec"]        = quality["video_codec"]
    if quality.get("audio_codec"):        update_dict["audio_codec"]        = quality["audio_codec"]
    if quality.get("audio_channels"):     update_dict["audio_channels"]     = quality["audio_channels"]
    if quality.get("audio_languages"):    update_dict["audio_languages"]    = quality["audio_languages"]
    if quality.get("subtitle_languages"): update_dict["subtitle_languages"] = quality["subtitle_languages"]
    if quality.get("file_path"):          update_dict["file_path"]          = quality["file_path"]

    file_stmt = pg_insert(CollectionFile).values(
        collection_id=collection_id,
        source=source,
        source_id=source_id,
        connection_id=connection_id,
        resolution=quality.get("resolution"),
        video_codec=quality.get("video_codec"),
        audio_codec=quality.get("audio_codec"),
        audio_channels=quality.get("audio_channels"),
        audio_languages=quality.get("audio_languages", []),
        subtitle_languages=quality.get("subtitle_languages", []),
        file_path=quality.get("file_path"),
    )
    if update_dict:
        file_stmt = file_stmt.on_conflict_do_update(
            constraint="uq_collection_file_source",
            set_=update_dict,
        )
    else:
        file_stmt = file_stmt.on_conflict_do_nothing(constraint="uq_collection_file_source")

    await db.execute(file_stmt)
    await db.flush()


async def _remove_collection_entry(
    db: AsyncSession,
    user_id: int,
    media_id: int,
    source: CollectionSource,
    source_id: str,
) -> None:
    """Removes the CollectionFile for this (source, source_id), and the parent
    Collection too if that was the last file backing it (an item can be
    collected from more than one source, e.g. both Plex and Jellyfin — only
    delisting it everywhere should remove it from the user's collection)."""
    from sqlalchemy import delete as sa_delete

    coll_result = await db.execute(
        select(Collection.id).where(Collection.user_id == user_id, Collection.media_id == media_id)
    )
    collection_id = coll_result.scalar_one_or_none()
    if collection_id is None:
        return

    await db.execute(
        sa_delete(CollectionFile).where(
            CollectionFile.collection_id == collection_id,
            CollectionFile.source == source,
            CollectionFile.source_id == source_id,
        )
    )
    await db.flush()

    remaining = await db.execute(
        select(CollectionFile.id).where(CollectionFile.collection_id == collection_id)
    )
    if remaining.first() is None:
        await db.execute(sa_delete(Collection).where(Collection.id == collection_id))
        await db.flush()


async def find_or_create_media_plex(
    data: dict, db: AsyncSession, api_key: str = None, conn: MediaServerConnection | None = None,
    user_id: int | None = None,
) -> Media | None:
    # Fastest path: match via CollectionFile source_id (plex ratingKey).
    # This works even after season remaps where show_id/season_number no longer
    # match what Plex reports in the webhook payload.
    if data.get("plex_rating_key"):
        cf_result = await db.execute(
            select(Media)
            .join(Collection, Collection.media_id == Media.id)
            .join(CollectionFile, CollectionFile.collection_id == Collection.id)
            .where(
                CollectionFile.source == CollectionSource.plex,
                CollectionFile.source_id == data["plex_rating_key"],
            )
        )
        media = cf_result.scalars().first()
        if media:
            return media

    series_tmdb_id: Optional[int] = int(data["grandparent_tmdb_id"]) if data.get("grandparent_tmdb_id") else None

    # If missing series_tmdb_id, try to resolve it via other identifiers
    if data["media_type"] == "episode" and not series_tmdb_id:
        # 1. Try grandparent TVDB/IMDb
        if data.get("grandparent_tvdb_id"):
            try:
                res = await tmdb.find_by_external_id(data["grandparent_tvdb_id"], "tvdb_id", api_key=api_key)
                if res.get("tv_results"):
                    series_tmdb_id = res["tv_results"][0]["id"]
            except Exception: pass

        if not series_tmdb_id and data.get("grandparent_imdb_id"):
            try:
                res = await tmdb.find_by_external_id(data["grandparent_imdb_id"], "imdb_id", api_key=api_key)
                if res.get("tv_results"):
                    series_tmdb_id = res["tv_results"][0]["id"]
            except Exception: pass

        # 2. Try episode identifiers (TMDB Find returns show context)
        if not series_tmdb_id and data.get("tvdb_id"):
            try:
                res = await tmdb.find_by_external_id(data["tvdb_id"], "tvdb_id", api_key=api_key)
                if res.get("tv_episode_results"):
                    series_tmdb_id = res["tv_episode_results"][0].get("show_id")
            except Exception: pass

        if not series_tmdb_id and data.get("imdb_id"):
            try:
                res = await tmdb.find_by_external_id(data["imdb_id"], "imdb_id", api_key=api_key)
                if res.get("tv_episode_results"):
                    series_tmdb_id = res["tv_episode_results"][0].get("show_id")
            except Exception: pass

        # 3. Fetch grandparent show from Plex to extract its TMDB GUID
        #    (needed when grandparentGuid is a plex://show/xxx internal ID)
        if not series_tmdb_id and data.get("grandparent_rating_key") and conn:
            try:
                import core.plex as plex_client
                show_item = await plex_client.get_item(conn.url, conn.token, data["grandparent_rating_key"])
                if show_item:
                    show_guids = show_item.get("Guid") or []
                    for g in show_guids:
                        gid = g.get("id", "")
                        if gid.startswith("tmdb://"):
                            try:
                                series_tmdb_id = int(gid.replace("tmdb://", ""))
                            except ValueError:
                                pass
                            break
                        elif re.search(r'themoviedb(?:\.com)?://(\d+)', gid, re.IGNORECASE):
                            m = re.search(r'themoviedb(?:\.com)?://(\d+)', gid, re.IGNORECASE)
                            if m:
                                try:
                                    series_tmdb_id = int(m.group(1))
                                except ValueError:
                                    pass
                            break
                    # Also try TVDB/IMDB on the show if TMDB still not found
                    if not series_tmdb_id:
                        for g in show_guids:
                            gid = g.get("id", "")
                            tvdb_m = re.search(r'(?:^tvdb|thetvdb(?:\.com)?)://(\d+)', gid, re.IGNORECASE)
                            if tvdb_m:
                                try:
                                    res = await tmdb.find_by_external_id(tvdb_m.group(1), "tvdb_id", api_key=api_key)
                                    if res.get("tv_results"):
                                        series_tmdb_id = res["tv_results"][0]["id"]
                                        break
                                except Exception:
                                    pass
            except Exception:
                pass

        # 4. Last resort: search by show title — exact name match only to avoid false positives
        #    (a fuzzy first-result on a show that doesn't exist on TMDB at all causes wrong linkage).
        if not series_tmdb_id and data.get("grandparent_title"):
            try:
                res = await tmdb.search_shows(data["grandparent_title"], api_key=api_key)
                gt_lower = data["grandparent_title"].lower()
                for r in (res.get("results") or [])[:3]:
                    if r.get("name", "").lower() == gt_lower or r.get("original_name", "").lower() == gt_lower:
                        series_tmdb_id = r["id"]
                        break
            except Exception: pass

    # When we couldn't verify the parent show on TMDB via any identifier, discard any
    # episode-level TMDB ID that Plex provided. Plex sometimes assigns a movie's TMDB ID
    # to episodes it can't match (the show exists only on TVDB/IMDB, not TMDB).
    if data["media_type"] == "episode" and not series_tmdb_id:
        data["tmdb_id"] = None

    if data["tmdb_id"]:
        tmdb_id_int = int(data["tmdb_id"])
        media_type = MediaType(data["media_type"])
        result = await db.execute(
            select(Media).where(
                Media.tmdb_id == tmdb_id_int,
                Media.media_type == media_type,
            )
        )
        media = result.scalars().first()
        if media:
            # Backfill show context if this episode record was created without it
            if media.media_type == MediaType.episode and media.show_id is None and series_tmdb_id:
                try:
                    show = await _find_or_create_show(db, series_tmdb_id, api_key)
                    media.show_id = show.id
                    tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, user_id)
                    await enrich_media(
                        media, api_key=api_key, series_tmdb_id=series_tmdb_id,
                        tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
                    )
                except Exception as e:
                    print(f"  Could not backfill show context for episode: {e}")
            return media

    # 2b. Movie matching by title + year if TMDB ID is missing
    if data["media_type"] == "movie" and not data["tmdb_id"]:
        # Try local match first to avoid redundant TMDB search
        local_q = select(Media).where(
            Media.media_type == MediaType.movie,
            Media.title.ilike(data["title"]),
        )
        if data.get("year"):
            local_q = local_q.where(Media.release_date.like(f"{data['year']}%"))
        
        media = (await db.execute(local_q)).scalars().first()
        if media:
            return media
            
        # Try TMDB search to find the real ID
        try:
            search_res = await tmdb.search_movies(data["title"], year=data.get("year"), api_key=api_key)
            if search_res.get("results"):
                tmdb_movie = search_res["results"][0]
                data["tmdb_id"] = str(tmdb_movie["id"])
                # Check again with the new TMDB ID
                result = await db.execute(
                    select(Media).where(
                        Media.tmdb_id == tmdb_movie["id"],
                        Media.media_type == MediaType.movie,
                    )
                )
                media = result.scalars().first()
                if media:
                    return media
        except Exception:
            pass

    # Don't create a row for an episode we can't identify at all — it can never
    # be enriched or matched back to a real episode, and would inflate collection counts.
    if data["media_type"] == "episode" and data["season_number"] is None and data["episode_number"] is None and not data["tmdb_id"]:
        print(f"  Skipping unidentifiable episode '{data['title']}' (no season/episode/tmdb_id)")
        return None

    # For episodes without a TMDB ID, look up by show+season+episode before creating
    # to avoid duplicate Media rows on repeated webhook events (e.g. episodes not yet
    # on TMDB that Plex tracks only by season/episode number).
    if data["media_type"] == "episode" and not data["tmdb_id"] and series_tmdb_id and data["season_number"] is not None and data["episode_number"] is not None:
        show_result = await db.execute(select(Show).where(Show.tmdb_id == series_tmdb_id))
        existing_show = show_result.scalar_one_or_none()
        if existing_show:
            ep_result = await db.execute(
                select(Media).where(
                    Media.show_id == existing_show.id,
                    Media.season_number == data["season_number"],
                    Media.episode_number == data["episode_number"],
                    Media.media_type == MediaType.episode,
                )
            )
            existing_ep = ep_result.scalars().first()
            if existing_ep:
                return existing_ep

    media, created = await create_media_safely(
        db,
        int(data["tmdb_id"]) if data["tmdb_id"] else None,
        MediaType(data["media_type"]),
        title=data["title"],
        season_number=data["season_number"],
        episode_number=data["episode_number"],
    )
    if created and media.media_type == MediaType.episode and not series_tmdb_id and data.get("grandparent_title"):
        media.tmdb_data = {"show_title": data["grandparent_title"]}

    if media.media_type == MediaType.episode and series_tmdb_id:
        try:
            show = await _find_or_create_show(db, series_tmdb_id, api_key)
            media.show_id = show.id
            tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, user_id)
            media = await enrich_media_safely(
                db, media, api_key=api_key, series_tmdb_id=series_tmdb_id,
                tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
            )
        except Exception as e:
            print(f"  Could not enrich episode with show context: {e}")
    else:
        await enrich_media(media, api_key=api_key)
    return media


async def _handle_plex_webhook(request: Request, db: AsyncSession, api_key: str, connection_id: int | None = None):
    user_result = await db.execute(select(User).where(User.api_key == api_key))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        form = await request.form()
    except Exception as e:
        return {"status": "error", "reason": f"form parse failed: {e}"}

    raw_payload = form.get("payload")
    if not raw_payload:
        return {"status": "ignored", "reason": "no payload field"}

    try:
        payload = json.loads(str(raw_payload))
    except (json.JSONDecodeError, TypeError):
        return {"status": "ignored", "reason": "invalid JSON"}

    event = payload.get("event", "unknown")

    data = parse_plex_payload(payload)
    if not data:
        return {"status": "ignored"}

    if connection_id is not None:
        conn = await _get_connection_by_id(db, user.id, connection_id)
    else:
        conn = await _get_oldest_connection(db, user.id, "plex")

    # If a plex server_username is configured on the connection, enforce it.
    account_title = data.get("account_title", "")
    if account_title and conn and conn.server_username:
        if account_title.lower() != conn.server_username.strip().lower():
            return {"status": "ignored", "reason": f"event for plex user '{account_title}' does not match connection '{conn.server_username}'"}

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    tmdb_key = await _get_tmdb_key(db, settings)

    session_key = f"plex:{user.id}:{data['session_key']}"

    if event in ("media.play", "media.resume", "media.pause", "media.stop", "media.scrobble", "media.rate"):
        if _is_duplicate_webhook_delivery(f"{session_key}:{event}"):
            return {"status": "ignored", "reason": "duplicate webhook delivery"}

    if event in ("media.play", "media.resume", "media.pause", "media.stop", "media.scrobble"):
        media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=conn, user_id=user.id)
        if media is None:
            return {"status": "ignored", "reason": "episode could not be identified (no season/episode/tmdb_id)"}

    if event == "media.play":
        if not conn or conn.sync_playback:
            session = await _get_or_open_session(db, session_key, "plex", user.id, media.id)
            session.state = "playing"
            session.updated_at = datetime.utcnow()
            if data["progress_percent"] > 0:
                session.progress_percent = data["progress_percent"]
                session.progress_seconds = data["progress_seconds"]
            if not media.runtime and data.get("duration_ms"):
                media.runtime = max(1, round(data["duration_ms"] / 60000))
            await db.commit()
        await _maybe_trakt_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_mdblist_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_simkl_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_bingebase_scrobble(settings, media, "start", data["progress_percent"], db=db)

    elif event == "media.resume":
        media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=conn, user_id=user.id)
        if media is None:
            return {"status": "ignored", "reason": "episode could not be identified (no season/episode/tmdb_id)"}
        if not conn or conn.sync_playback:
            session = await _get_or_open_session(db, session_key, "plex", user.id, media.id)
            session.state = "playing"
            session.progress_percent = data["progress_percent"]
            session.progress_seconds = data["progress_seconds"]
            session.updated_at = datetime.utcnow()
            if not media.runtime and data.get("duration_ms"):
                media.runtime = max(1, round(data["duration_ms"] / 60000))
            await db.commit()
        await _maybe_trakt_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_mdblist_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_simkl_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_bingebase_scrobble(settings, media, "start", data["progress_percent"], db=db)

    elif event == "media.pause":
        if not conn or conn.sync_playback:
            result = await db.execute(
                select(PlaybackSession).where(PlaybackSession.session_key == session_key)
            )
            session = result.scalar_one_or_none()
            if session:
                session.state = "paused"
                session.progress_percent = data["progress_percent"]
                session.progress_seconds = data["progress_seconds"]
                session.updated_at = datetime.utcnow()
                await db.commit()
        await _maybe_trakt_scrobble(settings, media, "pause", data["progress_percent"], db=db)
        await _maybe_mdblist_scrobble(settings, media, "pause", data["progress_percent"], db=db)
        await _maybe_bingebase_scrobble(settings, media, "pause", data["progress_percent"], db=db)

    elif event == "media.stop":
        session = await _close_session(db, session_key)
        progress_percent = data["progress_percent"] or (session.progress_percent if session else 0.0)
        if not conn or conn.sync_playback:
            progress_seconds = data["progress_seconds"] or (session.progress_seconds if session else 0)
            media_id = session.media_id if session else None
            if media_id is None:
                fallback = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=conn, user_id=user.id)
                media_id = fallback.id if fallback else None
            if media_id and (not conn or conn.sync_watched) and progress_percent > 0.05:
                await _write_watch_event(
                    db, user.id, media_id,
                    progress_percent, progress_seconds,
                    progress_percent >= 0.90,
                )
            await db.commit()
        await _maybe_trakt_scrobble(settings, media, "stop", progress_percent, db=db)
        await _maybe_mdblist_scrobble(settings, media, "stop", progress_percent, db=db)
        await _maybe_simkl_scrobble(settings, media, "stop", progress_percent, db=db)
        await _maybe_bingebase_scrobble(settings, media, "stop", progress_percent, db=db)

    elif event == "media.scrobble":
        await _close_session(db, session_key)
        if not conn or conn.sync_watched:
            media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=conn, user_id=user.id)
            if media:
                await _write_watch_event(db, user.id, media.id, 1.0, data["progress_seconds"], True)
            await db.commit()

    elif event == "media.rate":
        if not conn or conn.sync_ratings:
            media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=conn, user_id=user.id)
            rating_value = data.get("rating")

            existing = await db.execute(
                select(Rating).where(Rating.media_id == media.id, Rating.user_id == user.id)
            )
            existing_rating = existing.scalar_one_or_none()

            if rating_value is None or float(rating_value) == 0:
                if existing_rating:
                    await db.delete(existing_rating)
                    await db.commit()
            else:
                if existing_rating:
                    existing_rating.rating = float(rating_value)
                    existing_rating.rated_at = datetime.utcnow()
                else:
                    db.add(Rating(
                        media_id=media.id,
                        user_id=user.id,
                        rating=float(rating_value),
                    ))
                await db.commit()

    elif event == "library.new":
        if not conn or conn.sync_collection:
            section_id = data.get("library_section_id")
            if section_id and conn:
                sel_result = await db.execute(
                    select(PlexLibrarySelection).where(PlexLibrarySelection.connection_id == conn.id)
                )
                selected_keys = {row.library_key for row in sel_result.scalars().all()}
                if selected_keys and section_id not in selected_keys:
                    return {"status": "ignored", "reason": f"library section {section_id} not in sync selection"}

            import core.plex as plex_client

            plex_media_type = 1 if data["media_type"] == "movie" else 4
            recent_items: list = []
            if section_id and conn:
                recent_items = await plex_client.get_recently_added(
                    conn.url, conn.token, section_id, plex_media_type
                )

            payload_key = data.get("plex_rating_key")
            recent_keys = {str(it.get("ratingKey")) for it in recent_items}
            if payload_key:
                payload_item = await plex_client.get_item(conn.url, conn.token, payload_key) if conn else None
                if payload_item:
                    # Always prefer the individually-fetched item — bulk recentlyAdded
                    # omits Part.Stream data so audio/subtitle languages would be empty.
                    recent_items = [it for it in recent_items if str(it.get("ratingKey")) != str(payload_key)]
                    recent_items.insert(0, payload_item)
                elif str(payload_key) not in recent_keys:
                    recent_items = []

            if recent_items:
                for plex_item in recent_items:
                    item_guids = plex_item.get("Guid") or []
                    item_tmdb_id = plex_client.extract_tmdb_id(item_guids)
                    item_rating_key = str(plex_item.get("ratingKey", ""))
                    item_quality = plex_client.extract_quality(plex_item.get("Media", []))

                    item_data = {
                        "media_type": "movie" if plex_item.get("type") == "movie" else "episode",
                        "tmdb_id": str(item_tmdb_id) if item_tmdb_id else None,
                        "tvdb_id": plex_client.extract_tvdb_id(item_guids),
                        "imdb_id": plex_client.extract_imdb_id(item_guids),
                        "title": plex_item.get("title") or plex_item.get("grandparentTitle", ""),
                        "season_number": plex_item.get("parentIndex"),
                        "episode_number": plex_item.get("index"),
                        "plex_rating_key": item_rating_key,
                        "grandparent_rating_key": str(plex_item["grandparentRatingKey"]) if plex_item.get("grandparentRatingKey") else None,
                        "grandparent_title": plex_item.get("grandparentTitle"),
                        "grandparent_tmdb_id": None,
                        "grandparent_tvdb_id": None,
                        "grandparent_imdb_id": None,
                        "quality": item_quality,
                    }
                    gp_guid = plex_item.get("grandparentGuid", "")
                    m = re.search(r'(?:^tmdb|themoviedb(?:\.com)?)://(\d+)', gp_guid, re.IGNORECASE)
                    if m:
                        item_data["grandparent_tmdb_id"] = m.group(1)
                    m = re.search(r'(?:^tvdb|thetvdb(?:\.com)?)://(\d+)', gp_guid, re.IGNORECASE)
                    if m:
                        item_data["grandparent_tvdb_id"] = m.group(1)
                    m = re.search(r'imdb://(tt\d+)', gp_guid, re.IGNORECASE)
                    if m:
                        item_data["grandparent_imdb_id"] = m.group(1)

                    try:
                        item_media = await find_or_create_media_plex(
                            item_data, db, api_key=tmdb_key, conn=conn, user_id=user.id
                        )
                        if item_media:
                            await _ensure_collection_entry(
                                db, user.id, item_media.id, CollectionSource.plex,
                                item_rating_key, item_quality,
                                connection_id=conn.id if conn else None,
                            )
                    except Exception as e:
                        print(f"  library.new batch: failed to process item {item_rating_key}: {e}")
            else:
                media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=conn, user_id=user.id)
                quality = data.get("quality") or {}
                if (not quality.get("resolution") or not quality.get("audio_languages")) and conn:
                    item = await plex_client.get_item(conn.url, conn.token, data["plex_rating_key"])
                    if item:
                        quality = plex_client.extract_quality(item.get("Media", []))
                if media:
                    await _ensure_collection_entry(
                        db, user.id, media.id, CollectionSource.plex, data["plex_rating_key"], quality,
                        connection_id=conn.id if conn else None,
                    )
            await db.commit()

    elif event == "library.update":
        if not conn or conn.sync_collection:
            section_id = data.get("library_section_id")
            if section_id and conn:
                sel_result = await db.execute(
                    select(PlexLibrarySelection).where(PlexLibrarySelection.connection_id == conn.id)
                )
                selected_keys = {row.library_key for row in sel_result.scalars().all()}
                if selected_keys and section_id not in selected_keys:
                    return {"status": "ignored", "reason": f"library section {section_id} not in sync selection"}

            media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=conn, user_id=user.id)
            if media is None:
                return {"status": "ignored", "reason": "could not identify media"}

            quality = data.get("quality") or {}
            if (not quality.get("resolution") or not quality.get("audio_languages")) and conn:
                import core.plex as plex_client
                item = await plex_client.get_item(conn.url, conn.token, data["plex_rating_key"])
                if item:
                    quality = plex_client.extract_quality(item.get("Media", []))

            old_files_result = await db.execute(
                select(CollectionFile)
                .join(Collection)
                .where(
                    CollectionFile.source == CollectionSource.plex,
                    CollectionFile.source_id == data["plex_rating_key"],
                    Collection.user_id == user.id,
                    Collection.media_id != media.id,
                )
            )
            for old_file in old_files_result.scalars().all():
                old_collection_id = old_file.collection_id
                await db.delete(old_file)
                await db.flush()
                remaining = await db.execute(
                    select(func.count(CollectionFile.id)).where(
                        CollectionFile.collection_id == old_collection_id
                    )
                )
                if remaining.scalar() == 0:
                    old_coll = await db.get(Collection, old_collection_id)
                    if old_coll:
                        await db.delete(old_coll)

            await _ensure_collection_entry(
                db, user.id, media.id, CollectionSource.plex, data["plex_rating_key"], quality,
                connection_id=conn.id if conn else None,
            )
            await db.commit()

    return {"status": "ok", "event": event, "title": data["title"]}


async def _handle_plex_scrobble_webhook(request: Request, db: AsyncSession, api_key: str, connection_id: int):
    user_result = await db.execute(select(User).where(User.api_key == api_key))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        form = await request.form()
    except Exception as e:
        return {"status": "error", "reason": f"form parse failed: {e}"}

    raw_payload = form.get("payload")
    if not raw_payload:
        return {"status": "ignored", "reason": "no payload field"}

    try:
        payload = json.loads(str(raw_payload))
    except (json.JSONDecodeError, TypeError):
        return {"status": "ignored", "reason": "invalid JSON"}

    event = payload.get("event", "unknown")

    data = parse_plex_payload(payload)
    if not data:
        return {"status": "ignored"}

    conn = await _get_scrobble_connection_by_id(db, user.id, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Scrobble connection not found")

    account_title = data.get("account_title", "")
    if account_title and conn.server_username:
        if account_title.lower() != conn.server_username.strip().lower():
            return {"status": "ignored", "reason": f"event for plex user '{account_title}' does not match connection '{conn.server_username}'"}

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    tmdb_key = await _get_tmdb_key(db, settings)

    session_key = f"plex:scrobble:{user.id}:{data['session_key']}"

    if event in ("media.play", "media.resume", "media.pause", "media.stop", "media.scrobble"):
        if _is_duplicate_webhook_delivery(f"{session_key}:{event}"):
            return {"status": "ignored", "reason": "duplicate webhook delivery"}
        media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=None, user_id=user.id)
        if media is None:
            return {"status": "ignored", "reason": "episode could not be identified (no season/episode/tmdb_id)"}

    if event == "media.play":
        if conn.sync_playback:
            session = await _get_or_open_session(db, session_key, "plex", user.id, media.id)
            session.state = "playing"
            session.updated_at = datetime.utcnow()
            if data["progress_percent"] > 0:
                session.progress_percent = data["progress_percent"]
                session.progress_seconds = data["progress_seconds"]
            if not media.runtime and data.get("duration_ms"):
                media.runtime = max(1, round(data["duration_ms"] / 60000))
            await db.commit()

    elif event == "media.resume":
        media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=None, user_id=user.id)
        if media is None:
            return {"status": "ignored", "reason": "episode could not be identified (no season/episode/tmdb_id)"}
        if conn.sync_playback:
            session = await _get_or_open_session(db, session_key, "plex", user.id, media.id)
            session.state = "playing"
            session.progress_percent = data["progress_percent"]
            session.progress_seconds = data["progress_seconds"]
            session.updated_at = datetime.utcnow()
            if not media.runtime and data.get("duration_ms"):
                media.runtime = max(1, round(data["duration_ms"] / 60000))
            await db.commit()

    elif event == "media.pause":
        if conn.sync_playback:
            result = await db.execute(
                select(PlaybackSession).where(PlaybackSession.session_key == session_key)
            )
            session = result.scalar_one_or_none()
            if session:
                session.state = "paused"
                session.progress_percent = data["progress_percent"]
                session.progress_seconds = data["progress_seconds"]
                session.updated_at = datetime.utcnow()
                await db.commit()

    elif event == "media.stop":
        session = await _close_session(db, session_key)
        if conn.sync_playback:
            progress_percent = data["progress_percent"] or (session.progress_percent if session else 0.0)
            progress_seconds = data["progress_seconds"] or (session.progress_seconds if session else 0)
            if conn.sync_watched and progress_percent > 0.05:
                await _write_watch_event(db, user.id, media.id, progress_percent, progress_seconds, progress_percent >= 0.90)
        if conn.sync_collection:
            quality = data.get("quality")
            await _ensure_collection_entry(
                db, user.id, media.id, CollectionSource.plex, data["plex_rating_key"], quality,
                connection_id=None,
            )
        await db.commit()

    elif event == "media.scrobble":
        await _close_session(db, session_key)
        if conn.sync_watched:
            await _write_watch_event(db, user.id, media.id, 1.0, data["progress_seconds"], True)
        if conn.sync_collection:
            quality = data.get("quality")
            await _ensure_collection_entry(
                db, user.id, media.id, CollectionSource.plex, data["plex_rating_key"], quality,
                connection_id=None,
            )
        await db.commit()

    # library.new fires once, right when a title is added — often long before
    # anyone plays it, so "add to collection" can't wait on a playback event
    # (see #129). Unlike the full Plex connection's library.new handler, there's
    # no server URL/token here to re-fetch or check library selection against,
    # but parse_plex_payload already extracts everything needed (Guid array,
    # quality) straight from the webhook body itself.
    elif event == "library.new":
        if conn.sync_collection:
            media = await find_or_create_media_plex(data, db, api_key=tmdb_key, conn=None, user_id=user.id)
            if media:
                quality = data.get("quality")
                await _ensure_collection_entry(
                    db, user.id, media.id, CollectionSource.plex, data["plex_rating_key"], quality,
                    connection_id=None,
                )
                await db.commit()

    return {"status": "ok", "event": event, "title": data["title"]}


@router.post("/plex")
async def plex_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_plex_webhook(request, db, api_key)


@router.post("/plex/scrobble/{connection_id}")
async def plex_scrobble_webhook(
    connection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_plex_scrobble_webhook(request, db, api_key, connection_id)


@router.post("/plex/{connection_id}")
async def plex_webhook_connection(
    connection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_plex_webhook(request, db, api_key, connection_id)


# ── Kodi ───────────────────────────────────────────────────────────────────────

def parse_kodi_payload(payload: dict) -> dict | None:
    method = payload.get("method") or payload.get("event") or ""

    if method in ("Player.OnPlay", "playback_started"):
        notification_type = "play"
    elif method in ("Player.OnPause", "playback_paused"):
        notification_type = "pause"
    elif method in ("Player.OnResume", "playback_resumed"):
        notification_type = "resume"
    elif method in ("Player.OnStop", "playback_stopped"):
        notification_type = "stop"
    elif method in ("Player.OnAVChange", "playback_seeked"):
        notification_type = "progress"
    else:
        return None

    params_data = (payload.get("params") or {}).get("data") or {}
    addon_data = payload.get("data") or {}
    item = payload.get("item") or addon_data.get("item") or params_data.get("item") or {}
    player = payload.get("player") or addon_data.get("player") or params_data.get("player") or {}

    item_type = item.get("type", "")
    if item_type not in ("movie", "episode"):
        return None

    unique_ids = item.get("uniqueid") or {}
    tmdb_id = unique_ids.get("tmdb") or unique_ids.get("tmdbid") or item.get("tmdb_id")
    imdb_id = unique_ids.get("imdb") or item.get("imdbnumber")
    tvdb_id = unique_ids.get("tvdb")

    def hms_to_seconds(t: dict) -> int:
        return t.get("hours", 0) * 3600 + t.get("minutes", 0) * 60 + t.get("seconds", 0)

    time_info = player.get("time") or {}
    totaltime_info = player.get("totaltime") or {}
    position_seconds = int(payload.get("position_seconds") or hms_to_seconds(time_info))
    total_seconds = int(payload.get("total_seconds") or hms_to_seconds(totaltime_info) or item.get("runtime") or 0)
    progress_percent = round(position_seconds / total_seconds, 4) if total_seconds else 0.0

    ended = bool(params_data.get("end", False)) if method == "Player.OnStop" else False

    return {
        "notification_type": notification_type,
        "media_type": "movie" if item_type == "movie" else "episode",
        "title": item.get("title") or item.get("label") or "",
        "year": item.get("year"),
        "tmdb_id": str(tmdb_id) if tmdb_id else None,
        "imdb_id": str(imdb_id) if imdb_id else None,
        "tvdb_id": str(tvdb_id) if tvdb_id else None,
        "series_name": item.get("showtitle"),
        "season_number": item.get("season"),
        "episode_number": item.get("episode"),
        "progress_percent": progress_percent,
        "progress_seconds": position_seconds,
        "is_paused": notification_type == "pause",
        "ended": ended,
        "session_id": str(item.get("id") or payload.get("session_id") or "0"),
    }


async def find_or_create_media_kodi(
    data: dict, db: AsyncSession, api_key: str = None, user_id: int | None = None
) -> Media | None:
    series_tmdb_id: Optional[int] = None

    if data["media_type"] == "episode":
        if data.get("tvdb_id"):
            try:
                res = await tmdb.find_by_external_id(data["tvdb_id"], "tvdb_id", api_key=api_key)
                if res.get("tv_results"):
                    series_tmdb_id = res["tv_results"][0]["id"]
            except Exception:
                pass

        if not series_tmdb_id and data.get("imdb_id"):
            try:
                res = await tmdb.find_by_external_id(data["imdb_id"], "imdb_id", api_key=api_key)
                if res.get("tv_results"):
                    series_tmdb_id = res["tv_results"][0]["id"]
            except Exception:
                pass

        if not series_tmdb_id and data.get("series_name"):
            local = await db.execute(select(Show).where(Show.title.ilike(data["series_name"])))
            local_show = local.scalars().first()
            if local_show:
                series_tmdb_id = local_show.tmdb_id
            else:
                try:
                    res = await tmdb.search_shows(data["series_name"], api_key=api_key)
                    if res.get("results"):
                        series_tmdb_id = res["results"][0]["id"]
                except Exception:
                    pass

    show = None
    if series_tmdb_id:
        try:
            show = await _find_or_create_show(db, series_tmdb_id, api_key)
        except Exception:
            pass

    if data.get("tmdb_id"):
        result = await db.execute(
            select(Media).where(
                Media.tmdb_id == int(data["tmdb_id"]),
                Media.media_type == MediaType(data["media_type"]),
            )
        )
        media = result.scalars().first()
        if media:
            if media.media_type == MediaType.episode and media.show_id is None and show:
                media.show_id = show.id
                tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, user_id)
                await enrich_media(
                    media, api_key=api_key, series_tmdb_id=series_tmdb_id,
                    tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
                )
            return media

    if data["media_type"] == "movie":
        local_q = select(Media).where(Media.media_type == MediaType.movie, Media.title.ilike(data["title"]))
        if data.get("year"):
            local_q = local_q.where(Media.release_date.like(f"{data['year']}%"))
        media = (await db.execute(local_q)).scalars().first()
        if media:
            return media
        try:
            search_res = await tmdb.search_movies(data["title"], year=data.get("year"), api_key=api_key)
            if search_res.get("results"):
                tmdb_movie = search_res["results"][0]
                data["tmdb_id"] = str(tmdb_movie["id"])
                result = await db.execute(
                    select(Media).where(Media.tmdb_id == tmdb_movie["id"], Media.media_type == MediaType.movie)
                )
                media = result.scalars().first()
                if media:
                    return media
        except Exception:
            pass

    if (
        data["media_type"] == "episode"
        and show
        and data.get("season_number") is not None
        and data.get("episode_number") is not None
    ):
        result = await db.execute(
            select(Media).where(
                Media.media_type == MediaType.episode,
                Media.show_id == show.id,
                Media.season_number == data["season_number"],
                Media.episode_number == data["episode_number"],
            )
        )
        media = result.scalars().first()
        if media:
            return media

    if data["media_type"] == "episode" and not data.get("tmdb_id") and data.get("season_number") is None:
        return None

    media, _created = await create_media_safely(
        db,
        int(data["tmdb_id"]) if data.get("tmdb_id") else None,
        MediaType(data["media_type"]),
        title=data["title"],
        season_number=data.get("season_number"),
        episode_number=data.get("episode_number"),
        show_id=show.id if show else None,
    )
    if show and series_tmdb_id:
        tvdb_id, tvdb_api_key, tvdb_lang = await _resolve_tvdb_fallback(db, show, user_id)
        media = await enrich_media_safely(
            db, media, api_key=api_key, series_tmdb_id=series_tmdb_id,
            tvdb_id=tvdb_id, tvdb_api_key=tvdb_api_key, tvdb_lang=tvdb_lang,
        )
    else:
        await enrich_media(media, api_key=api_key)
    return media


async def _handle_kodi_webhook(request: Request, db: AsyncSession, api_key: str):
    user_result = await db.execute(select(User).where(User.api_key == api_key))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.body()
    if not body:
        return {"status": "ignored", "reason": "empty body"}

    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "invalid JSON"}

    data = parse_kodi_payload(payload)
    if not data:
        return {"status": "ignored"}

    notification_type = data["notification_type"]

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    tmdb_key = await _get_tmdb_key(db, settings)

    media = await find_or_create_media_kodi(data, db, api_key=tmdb_key, user_id=user.id)
    if media is None:
        return {"status": "ignored", "reason": "could not identify media"}

    session_key = f"kodi:{user.id}:{data['session_id']}"

    if notification_type == "play":
        session = await _get_or_open_session(db, session_key, "kodi", user.id, media.id)
        session.state = "playing"
        session.updated_at = datetime.utcnow()
        await db.commit()
        await _maybe_trakt_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_mdblist_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_simkl_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_bingebase_scrobble(settings, media, "start", data["progress_percent"], db=db)

    elif notification_type == "resume":
        session = await _get_or_open_session(db, session_key, "kodi", user.id, media.id)
        session.state = "playing"
        session.progress_percent = data["progress_percent"]
        session.progress_seconds = data["progress_seconds"]
        session.updated_at = datetime.utcnow()
        await db.commit()
        await _maybe_trakt_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_mdblist_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_simkl_scrobble(settings, media, "start", data["progress_percent"], db=db)
        await _maybe_bingebase_scrobble(settings, media, "start", data["progress_percent"], db=db)

    elif notification_type == "pause":
        result = await db.execute(select(PlaybackSession).where(PlaybackSession.session_key == session_key))
        session = result.scalar_one_or_none()
        if session:
            session.state = "paused"
            session.progress_percent = data["progress_percent"]
            session.progress_seconds = data["progress_seconds"]
            session.updated_at = datetime.utcnow()
            await db.commit()
        await _maybe_trakt_scrobble(settings, media, "pause", data["progress_percent"], db=db)
        await _maybe_mdblist_scrobble(settings, media, "pause", data["progress_percent"], db=db)
        await _maybe_bingebase_scrobble(settings, media, "pause", data["progress_percent"], db=db)

    elif notification_type == "progress":
        session = await _get_or_open_session(db, session_key, "kodi", user.id, media.id)
        session.state = "paused" if data["is_paused"] else "playing"
        session.progress_percent = data["progress_percent"]
        session.progress_seconds = data["progress_seconds"]
        session.updated_at = datetime.utcnow()
        await db.commit()

    elif notification_type == "stop":
        session = await _close_session(db, session_key)
        progress_percent = data["progress_percent"] or (session.progress_percent if session else 0.0)
        progress_seconds = data["progress_seconds"] or (session.progress_seconds if session else 0)
        completed = data.get("ended") or progress_percent >= 0.90
        if completed or progress_percent > 0.05:
            await _write_watch_event(db, user.id, media.id, progress_percent, progress_seconds, completed)
        await db.commit()
        await _maybe_trakt_scrobble(settings, media, "stop", progress_percent, db=db)
        await _maybe_mdblist_scrobble(settings, media, "stop", progress_percent, db=db)
        await _maybe_simkl_scrobble(settings, media, "stop", progress_percent, db=db)
        await _maybe_bingebase_scrobble(settings, media, "stop", progress_percent, db=db)

    return {"status": "ok", "event": notification_type, "title": data["title"]}


@router.post("/kodi")
async def kodi_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    return await _handle_kodi_webhook(request, db, api_key)


@router.get("/kodi/history")
async def kodi_library_history(
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    user_result = await db.execute(select(User).where(User.api_key == api_key))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    movie_rows = (await db.execute(
        select(Media.tmdb_id, func.sum(WatchEvent.play_count).label("play_count"))
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == user.id,
            Media.media_type == MediaType.movie,
            Media.tmdb_id.isnot(None),
        )
        .group_by(Media.tmdb_id)
    )).all()

    episode_rows = (await db.execute(
        select(Show.tmdb_id, Media.season_number, Media.episode_number, func.sum(WatchEvent.play_count).label("play_count"))
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .join(Show, Show.id == Media.show_id)
        .where(
            WatchEvent.user_id == user.id,
            Media.media_type == MediaType.episode,
            Media.season_number.isnot(None),
            Media.episode_number.isnot(None),
        )
        .group_by(Show.tmdb_id, Media.season_number, Media.episode_number)
    )).all()

    return {
        "movies": [{"tmdb_id": r.tmdb_id, "play_count": r.play_count} for r in movie_rows],
        "episodes": [{"show_tmdb_id": r.tmdb_id, "season_number": r.season_number, "episode_number": r.episode_number, "play_count": r.play_count} for r in episode_rows],
    }


class KodiRatingPayload(BaseModel):
    tmdb_id: Optional[str] = None
    tvdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None
    media_type: str
    rating: float
    series_name: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None


@router.post("/kodi/rating")
async def kodi_rating(
    payload: KodiRatingPayload,
    db: AsyncSession = Depends(get_db),
    api_key: str = Query(..., description="Scrob user API key"),
):
    user_result = await db.execute(select(User).where(User.api_key == api_key))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    tmdb_key = await _get_tmdb_key(db, settings)

    data = {
        "media_type": payload.media_type,
        "title": payload.title or "",
        "year": payload.year,
        "tmdb_id": payload.tmdb_id,
        "imdb_id": payload.imdb_id,
        "tvdb_id": payload.tvdb_id,
        "series_name": payload.series_name,
        "season_number": payload.season_number,
        "episode_number": payload.episode_number,
    }
    media = await find_or_create_media_kodi(data, db, api_key=tmdb_key, user_id=user.id)
    if media is None:
        raise HTTPException(status_code=422, detail="Could not identify media")

    existing_result = await db.execute(
        select(Rating).where(Rating.media_id == media.id, Rating.user_id == user.id)
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        existing.rating = payload.rating
        existing.rated_at = datetime.utcnow()
    else:
        db.add(Rating(media_id=media.id, user_id=user.id, rating=payload.rating))
    await db.commit()
    return {"status": "ok"}

