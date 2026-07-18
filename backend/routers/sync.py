import asyncio
import logging
import re
from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select, update, delete, func, cast
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.dialects.postgresql import insert, JSONB

from db import get_db, engine
from models.media import Media
from models.show import Show
from models.collection import Collection, CollectionFile
from models.users import User, UserSettings
from models.connections import MediaServerConnection
from models.sync import SyncJob, SyncStatus
from models.events import WatchEvent
from models.ratings import Rating, RatingChanges, RatingKey
from models.playback_progress import PlaybackProgress
from models.library_selections import JellyfinLibrarySelection, EmbyLibrarySelection, PlexLibrarySelection
from models.season_override import ShowSeasonOverride
from datetime import datetime, timezone
from dateutil import parser
from models.base import MediaType, CollectionSource
from models.global_settings import GlobalSettings
from core import jellyfin, emby, plex, nuvio, tmdb
import core.trakt as trakt_client
from core.enrichment import enrich_media
from core.image_cache import pre_cache_all_collected_bg

from dependencies import get_current_user
logger = logging.getLogger("uvicorn.error")



async def _get_effective_tmdb_key(db: AsyncSession, user_settings: UserSettings | None) -> str | None:
    if user_settings and user_settings.tmdb_api_key:
        return user_settings.tmdb_api_key
    gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = gs_result.scalar_one_or_none()
    return gs.tmdb_api_key if gs else None

router = APIRouter()

# Global semaphore — at most one sync running at a time across all users
_sync_semaphore = asyncio.Semaphore(1)

BATCH_SIZE = 500
TMDB_CONCURRENCY = 5  # Max concurrent TMDB requests
# asyncpg hard limit is 32767 parameters per query; stay well under it
_MAX_IN_PARAMS = 30_000
_MEDIA_BROWSER_ITEM_SOURCES = (CollectionSource.jellyfin, CollectionSource.emby, CollectionSource.nuvio)


async def _select_in_chunks(db: AsyncSession, stmt_builder, ids: list):
    """Execute a select statement using chunked IN clauses to avoid the 32767-parameter limit.
    stmt_builder(chunk) should return a SQLAlchemy select() statement for that chunk of IDs.
    Returns a flat list of all rows."""
    results = []
    for i in range(0, len(ids), _MAX_IN_PARAMS):
        chunk = ids[i : i + _MAX_IN_PARAMS]
        res = await db.execute(stmt_builder(chunk))
        results.extend(res.scalars().all())
    return results


async def _latest_watched_at(db: AsyncSession, user_id: int, media_ids: list) -> dict:
    """Latest completed WatchEvent.watched_at per media_id, chunked to avoid the 32767-parameter limit."""
    watched_at_by_media: dict[int, datetime] = {}
    for i in range(0, len(media_ids), _MAX_IN_PARAMS):
        chunk = media_ids[i : i + _MAX_IN_PARAMS]
        result = await db.execute(
            select(WatchEvent.media_id, WatchEvent.watched_at)
            .where(
                WatchEvent.user_id == user_id,
                WatchEvent.media_id.in_(chunk),
                WatchEvent.completed == True,
            )
            .order_by(WatchEvent.watched_at.desc())
        )
        for media_id, watched_at in result.all():
            watched_at_by_media.setdefault(media_id, watched_at)
    return watched_at_by_media


async def _resolve_tmdb_season_ids(
    media_by_id: dict[int, Media],
    rating_keys: set[RatingKey],
    api_key: str | None,
) -> dict[RatingKey, int]:
    """Resolve TMDB season resource IDs for season rating operations."""
    season_keys = {
        key
        for key in rating_keys
        if key[1] is not None
        and (media := media_by_id.get(key[0]))
        and media.media_type == MediaType.series
        and media.tmdb_id
    }
    if not season_keys:
        return {}

    resolved: dict[RatingKey, int] = {}
    semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

    async def resolve(key: RatingKey) -> None:
        media = media_by_id[key[0]]
        async with semaphore:
            try:
                season = await tmdb.get_season(
                    media.tmdb_id,
                    key[1],
                    api_key=api_key,
                )
            except Exception as exc:
                logger.warning(
                    "Could not resolve TMDB season ID for show=%s season=%s: %s",
                    media.tmdb_id,
                    key[1],
                    exc,
                )
                return
        season_tmdb_id = season.get("id")
        if season_tmdb_id:
            resolved[key] = int(season_tmdb_id)

    await asyncio.gather(*(resolve(key) for key in season_keys))
    return resolved


async def _get_or_create_series_rating_media(
    db: AsyncSession,
    tmdb_id: int,
    title: str,
    api_key: str | None,
) -> Media:
    result = await db.execute(
        select(Media).where(
            Media.tmdb_id == tmdb_id,
            Media.media_type == MediaType.series,
        )
    )
    media = result.scalars().first()
    if media:
        return media
    media = Media(tmdb_id=tmdb_id, media_type=MediaType.series, title=title)
    db.add(media)
    await db.flush()
    await enrich_media(media, api_key=api_key)
    return media


def extract_watch_state(item: dict, source: CollectionSource) -> dict:
    state = {"completed": False, "last_played": None, "play_count": 0, "user_rating": None}

    if source in _MEDIA_BROWSER_ITEM_SOURCES:
        user_data = item.get("UserData", {})
        state["completed"] = user_data.get("Played", False)
        state["play_count"] = user_data.get("PlayCount", 1 if state["completed"] else 0)
        lp = user_data.get("LastPlayedDate")
        if lp:
            dt = parser.isoparse(lp)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            state["last_played"] = dt
        r = user_data.get("Rating")
        if r is not None:
            state["user_rating"] = float(r)
    else:  # Plex
        state["play_count"] = int(item.get("viewCount", 0))
        state["completed"] = state["play_count"] > 0
        lp = item.get("lastViewedAt")
        if lp:
            state["last_played"] = datetime.fromtimestamp(lp, tz=timezone.utc).replace(tzinfo=None)
        r = item.get("userRating")
        if r is not None:
            state["user_rating"] = float(r)

    return state


def get_jellyfin_tmdb_id(provider_ids: dict) -> int | None:
    tid = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
    return int(tid) if tid else None


def extract_jellyfin_quality(item: dict) -> dict:
    from core.jellyfin import extract_quality
    quality = extract_quality(item.get("MediaStreams", []))
    quality["file_path"] = item.get("Path")
    return quality


async def sync_shows_batch(
    series_tmdb_map: dict,  # source_series_id → tmdb_id
    db: AsyncSession,
    api_key: str = None,
) -> tuple[dict, dict]:
    """
    Fetch and insert all shows in parallel (up to TMDB_CONCURRENCY concurrent requests).
    Returns (show_map: source_id→show.id, show_id_to_tmdb: show.id→series_tmdb_id).
    """
    all_tmdb_ids = list({tid for tid in series_tmdb_map.values() if tid})

    # Bulk load already-known shows (chunked to stay under asyncpg's 32767-param limit)
    existing_shows: dict[int, Show] = {}
    if all_tmdb_ids:
        shows_loaded = await _select_in_chunks(
            db,
            lambda chunk: select(Show).where(Show.tmdb_id.in_(chunk)),
            all_tmdb_ids,
        )
        for s in shows_loaded:
            existing_shows[s.tmdb_id] = s

    missing = [tid for tid in all_tmdb_ids if tid not in existing_shows]

    # Also re-fetch active shows so new seasons added to TMDB appear without a manual refresh.
    ACTIVE_STATUSES = {"Returning Series", "In Production", "Planned"}
    stale = [
        tid for tid in all_tmdb_ids
        if tid in existing_shows and existing_shows[tid].status in ACTIVE_STATUSES
    ]
    to_fetch = list({*missing, *stale})
    print(f"    {len(existing_shows)} shows in DB, fetching {len(missing)} new + {len(stale)} active from TMDB...")

    semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)
    fetched: dict[int, dict] = {}

    async def fetch_show(tmdb_id: int):
        async with semaphore:
            try:
                fetched[tmdb_id] = await tmdb.get_show(tmdb_id, api_key=api_key)
            except Exception as e:
                print(f"  Failed to fetch show tmdb={tmdb_id}: {e}")

    if to_fetch:
        await asyncio.gather(*[fetch_show(tid) for tid in to_fetch])

    if fetched:
        values = []
        for tmdb_id, d in fetched.items():
            values.append({
                "tmdb_id": tmdb_id,
                "title": d.get("name"),
                "original_title": d.get("original_name"),
                "overview": d.get("overview"),
                "poster_path": tmdb.poster_url(d.get("poster_path")),
                "backdrop_path": tmdb.poster_url(d.get("backdrop_path"), size="w1280"),
                "tmdb_rating": d.get("vote_average"),
                "status": d.get("status"),
                "tagline": d.get("tagline"),
                "first_air_date": d.get("first_air_date"),
                "last_air_date": d.get("last_air_date"),
                "tmdb_data": {
                    "genres": [g["name"] for g in d.get("genres", [])],
                    "external_ids": d.get("external_ids", {}),
                    "original_language": d.get("original_language"),
                    "seasons": [
                        {
                            "season_number": s["season_number"],
                            "poster_path": tmdb.poster_url(s.get("poster_path")),
                            "episode_count": s["episode_count"],
                            "name": s["name"],
                            "overview": s.get("overview"),
                            "air_date": s.get("air_date"),
                        }
                        for s in d.get("seasons", [])
                    ],
                },
            })

        # Show has 12 value columns; 32767 / 12 = 2730 rows max per statement.
        # Use BATCH_SIZE (500) to stay well under the asyncpg 32767-parameter limit.
        update_cols = [k for k in values[0].keys() if k != "tmdb_id"]
        for i in range(0, len(values), BATCH_SIZE):
            chunk = values[i : i + BATCH_SIZE]
            stmt = insert(Show).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["tmdb_id"],
                set_={k: getattr(stmt.excluded, k) for k in update_cols},
            )
            stmt = stmt.returning(Show)
            res = await db.execute(stmt)
            for s in res.scalars().all():
                existing_shows[s.tmdb_id] = s

    show_map: dict[str, int] = {}
    show_id_to_tmdb: dict[int, int] = {}
    for source_id, tmdb_id in series_tmdb_map.items():
        show = existing_shows.get(tmdb_id)
        if show:
            show_map[str(source_id)] = show.id
            show_id_to_tmdb[show.id] = show.tmdb_id

    return show_map, show_id_to_tmdb


async def batch_enrich_items(
    items: list[tuple],  # (Media, series_tmdb_id | None)
    api_key: str = None,
    show_title_map: dict[int, str] | None = None,
) -> list[dict]:
    """
    Parallel enrichment for newly created media.
    Episodes: one TMDB /season/{n} call per unique season (3865 calls vs 45k).
    Movies: parallel /movie/{id} calls.
    Returns a list of warning dicts for seasons/items that couldn't be enriched.
    """
    semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)
    if show_title_map is None:
        show_title_map = {}

    movies = [m for (m, _) in items if m.media_type == MediaType.movie]
    episodes = [(m, stid) for (m, stid) in items if m.media_type == MediaType.episode and stid]

    # ── Movies: parallel enrichment ──────────────────────────────────────────
    async def enrich_movie(media: Media):
        async with semaphore:
            await enrich_media(media, api_key=api_key)

    if movies:
        await asyncio.gather(*[enrich_movie(m) for m in movies], return_exceptions=True)

    # ── Episodes: one TMDB call per unique (series, season) ──────────────────
    season_to_eps: dict[tuple, list[Media]] = {}
    for media, stid in episodes:
        if media.season_number is not None:
            season_to_eps.setdefault((stid, media.season_number), []).append(media)

    season_data: dict[tuple, dict[int, dict]] = {}
    failed_season_keys: set[tuple] = set()

    async def fetch_season(stid: int, sn: int):
        async with semaphore:
            try:
                d = await tmdb.get_season(stid, sn, api_key=api_key)
                season_data[(stid, sn)] = {ep["episode_number"]: ep for ep in d.get("episodes", [])}
            except Exception as e:
                print(f"  Failed to fetch show={stid} season={sn}: {e}")
                season_data[(stid, sn)] = {}
                failed_season_keys.add((stid, sn))

    if season_to_eps:
        print(f"    Fetching {len(season_to_eps)} seasons from TMDB...")
        await asyncio.gather(
            *[fetch_season(stid, sn) for (stid, sn) in season_to_eps],
            return_exceptions=True,
        )

    for (stid, sn), ep_list in season_to_eps.items():
        ep_map = season_data.get((stid, sn), {})
        for media in ep_list:
            ep = ep_map.get(media.episode_number)
            if not ep:
                continue
            media.tmdb_id = ep.get("id") or media.tmdb_id
            media.title = ep.get("name") or media.title
            media.overview = ep.get("overview")
            media.poster_path = tmdb.poster_url(ep.get("still_path"), size="w500")
            media.release_date = ep.get("air_date")
            media.tmdb_rating = ep.get("vote_average")
            media.tmdb_data = {"runtime": ep.get("runtime"), "cast": []}

    # Build per-season warning entries (one entry per failed season)
    warnings: list[dict] = []
    for (stid, sn) in sorted(failed_season_keys):
        warnings.append({
            "show": show_title_map.get(stid, f"TMDB show #{stid}"),
            "tmdb_id": stid,
            "season": sn,
            "affected_episodes": len(season_to_eps.get((stid, sn), [])),
            "reason": "Season not found on TMDB — the show may be split into separate series on TMDB",
        })

    return warnings


def _nuvio_profile_id(conn: MediaServerConnection) -> int:
    return nuvio.parse_profile_id(conn.server_user_id)


def _nuvio_imdb_id(entity: Media | Show | None) -> str | None:
    if entity is None:
        return None
    data = entity.tmdb_data or {}
    value = data.get("imdb_id") or (data.get("external_ids") or {}).get("imdb_id")
    imdb_id = str(value or "").strip()
    return imdb_id if imdb_id.startswith("tt") and imdb_id[2:].isdigit() else None


async def _ensure_nuvio_imdb_ids(
    media_rows: list[Media],
    shows_by_id: dict[int, Show],
    api_key: str | None,
) -> None:
    if not api_key:
        return

    targets: dict[tuple[str, int], Media | Show] = {}
    for media in media_rows:
        if media.media_type == MediaType.episode:
            show = shows_by_id.get(media.show_id)
            if show and show.tmdb_id and not _nuvio_imdb_id(show):
                targets[("tv", show.tmdb_id)] = show
        elif media.tmdb_id and not _nuvio_imdb_id(media):
            target_type = "movie" if media.media_type == MediaType.movie else "tv"
            targets[(target_type, media.tmdb_id)] = media
    if not targets:
        return

    semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

    async def fetch_imdb_id(target_type: str, tmdb_id: int, entity: Media | Show) -> None:
        async with semaphore:
            try:
                external_ids = await tmdb.get_external_ids(tmdb_id, target_type, api_key=api_key)
            except Exception as exc:
                logger.warning(
                    "Failed to resolve outbound Nuvio IMDb ID for TMDB %s (%s): %s",
                    tmdb_id,
                    target_type,
                    exc,
                )
                return
        imdb_id = str(external_ids.get("imdb_id") or "").strip()
        if not (imdb_id.startswith("tt") and imdb_id[2:].isdigit()):
            return
        tmdb_data = dict(entity.tmdb_data or {})
        stored_external_ids = dict(tmdb_data.get("external_ids") or {})
        stored_external_ids["imdb_id"] = imdb_id
        tmdb_data["external_ids"] = stored_external_ids
        entity.tmdb_data = tmdb_data

    await asyncio.gather(
        *[
            fetch_imdb_id(target_type, tmdb_id, entity)
            for (target_type, tmdb_id), entity in targets.items()
        ]
    )
    logger.info("Resolved %s outbound Nuvio IMDb identifiers through TMDB", len(targets))


def _nuvio_watched_item(
    media: Media,
    watched_at: datetime,
    show: Show | None = None,
) -> dict | None:
    if watched_at.tzinfo is None:
        watched_at = watched_at.replace(tzinfo=timezone.utc)
    watched_epoch_ms = int(watched_at.timestamp() * 1000)

    if media.media_type == MediaType.movie and (content_id := _nuvio_imdb_id(media)):
        return {
            "content_id": content_id,
            "content_type": "movie",
            "title": media.title,
            "watched_at": watched_epoch_ms,
        }
    if (
        media.media_type == MediaType.episode
        and (content_id := _nuvio_imdb_id(show))
        and media.season_number is not None
        and media.episode_number is not None
    ):
        return {
            "content_id": content_id,
            "content_type": "series",
            "title": media.title,
            "season": media.season_number,
            "episode": media.episode_number,
            "watched_at": watched_epoch_ms,
        }
    if media.media_type == MediaType.series and (content_id := _nuvio_imdb_id(media)):
        return {
            "content_id": content_id,
            "content_type": "series",
            "title": media.title,
            "watched_at": watched_epoch_ms,
        }
    return None


async def _build_nuvio_watched_items(
    db: AsyncSession,
    user_id: int,
    media_ids: set[int] | None = None,
    api_key: str | None = None,
) -> list[dict]:
    event_query = (
        select(WatchEvent.media_id, WatchEvent.watched_at)
        .where(WatchEvent.user_id == user_id, WatchEvent.completed == True)
        .order_by(WatchEvent.watched_at.desc())
    )
    if media_ids is not None:
        if not media_ids:
            return []
        event_query = event_query.where(WatchEvent.media_id.in_(media_ids))
    event_result = await db.execute(event_query)
    latest_watched_at: dict[int, datetime] = {}
    for media_id, watched_at in event_result.all():
        latest_watched_at.setdefault(media_id, watched_at)
    if not latest_watched_at:
        return []

    media_rows = await _select_in_chunks(
        db,
        lambda chunk: select(Media).where(Media.id.in_(chunk)),
        list(latest_watched_at),
    )
    show_ids = {media.show_id for media in media_rows if media.show_id is not None}
    shows_by_id: dict[int, Show] = {}
    if show_ids:
        shows = await _select_in_chunks(
            db,
            lambda chunk: select(Show).where(Show.id.in_(chunk)),
            list(show_ids),
        )
        shows_by_id = {show.id: show for show in shows}

    await _ensure_nuvio_imdb_ids(media_rows, shows_by_id, api_key)
    items: list[dict] = []
    for media in media_rows:
        item = _nuvio_watched_item(
            media,
            latest_watched_at[media.id],
            shows_by_id.get(media.show_id),
        )
        if item:
            items.append(item)
    return items


def _nuvio_progress_item(
    progress: PlaybackProgress,
    media: Media,
    show: Show | None = None,
) -> dict | None:
    try:
        progress_seconds = max(0, int(progress.progress_seconds))
        progress_percent = float(progress.progress_percent)
    except (TypeError, ValueError):
        return None
    if progress_seconds <= 0 or progress_percent <= 0:
        return None

    position_ms = progress_seconds * 1000
    if media.runtime and media.runtime > 0:
        duration_ms = media.runtime * 60_000
    else:
        duration_ms = round(position_ms / max(min(progress_percent, 1.0), 0.01))
    duration_ms = max(position_ms, duration_ms)

    updated_at = progress.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    last_watched = int(updated_at.timestamp() * 1000)

    if media.media_type == MediaType.movie and (content_id := _nuvio_imdb_id(media)):
        return {
            "content_id": content_id,
            "content_type": "movie",
            "video_id": content_id,
            "position": position_ms,
            "duration": duration_ms,
            "last_watched": last_watched,
        }
    if (
        media.media_type == MediaType.episode
        and (content_id := _nuvio_imdb_id(show))
        and media.season_number is not None
        and media.episode_number is not None
    ):
        return {
            "content_id": content_id,
            "content_type": "series",
            "video_id": f"{content_id}:{media.season_number}:{media.episode_number}",
            "season": media.season_number,
            "episode": media.episode_number,
            "position": position_ms,
            "duration": duration_ms,
            "last_watched": last_watched,
        }
    return None


async def _build_nuvio_progress_items(
    db: AsyncSession,
    user_id: int,
    api_key: str | None = None,
) -> list[dict]:
    result = await db.execute(
        select(PlaybackProgress, Media)
        .join(Media, Media.id == PlaybackProgress.media_id)
        .where(PlaybackProgress.user_id == user_id)
        .order_by(Media.id)
    )
    rows = result.all()
    show_ids = {
        media.show_id
        for _, media in rows
        if media.media_type == MediaType.episode and media.show_id is not None
    }
    shows_by_id: dict[int, Show] = {}
    if show_ids:
        shows_result = await db.execute(select(Show).where(Show.id.in_(show_ids)))
        shows_by_id = {show.id: show for show in shows_result.scalars().all()}
    await _ensure_nuvio_imdb_ids([media for _, media in rows], shows_by_id, api_key)

    items: list[dict] = []
    for progress, media in rows:
        item = _nuvio_progress_item(progress, media, shows_by_id.get(media.show_id))
        if item:
            items.append(item)
    return items


async def _fan_out_changes_to_other_connections(
    db: AsyncSession,
    user_id: int,
    exclude_connection_id: int | None,
    new_watched_ids: set[int],
    new_ratings: RatingChanges,
    settings: "UserSettings | None" = None,
    exclude_cloud_source: CollectionSource | None = None,
    removed_ratings: set[RatingKey] | None = None,
) -> None:
    """Push an inbound sync delta to every enabled media server and cloud target.

    ``exclude_connection_id`` prevents media-server echo. ``exclude_cloud_source``
    prevents a cloud pull from writing the same delta back to its source.
    """
    removed_ratings = removed_ratings or set()
    if not new_watched_ids and not new_ratings and not removed_ratings:
        return

    all_changed_ids = (
        set(new_watched_ids)
        | {media_id for media_id, _ in new_ratings}
        | {media_id for media_id, _ in removed_ratings}
    )
    media_items = await _select_in_chunks(
        db,
        lambda chunk: select(Media).where(Media.id.in_(chunk)),
        list(all_changed_ids),
    )
    media_by_id: dict[int, Media] = {media.id: media for media in media_items}

    # ── Media server fan-out ─────────────────────────────────────────────────
    conns_filter = [MediaServerConnection.user_id == user_id]
    if exclude_connection_id is not None:
        conns_filter.append(MediaServerConnection.id != exclude_connection_id)
    other_conns_result = await db.execute(
        select(MediaServerConnection).where(*conns_filter)
    )
    other_conns = other_conns_result.scalars().all()
    push_candidates = [c for c in other_conns if c.push_watched or c.push_ratings]

    push_tasks = []
    server_rating_changes = {key: 0.0 for key in removed_ratings}
    server_rating_changes.update(new_ratings)

    if push_candidates:
        # Chunk the IN clause to stay under asyncpg's 32767-parameter limit.
        # A large first-time sync can produce tens of thousands of changed IDs.
        source_ids_map: dict[tuple[CollectionSource, int], list[str]] = {}
        all_changed_list = list(all_changed_ids)
        for i in range(0, len(all_changed_list), _MAX_IN_PARAMS):
            chunk = all_changed_list[i : i + _MAX_IN_PARAMS]
            files_result = await db.execute(
                select(CollectionFile.source_id, CollectionFile.source, Collection.media_id)
                .join(Collection, Collection.id == CollectionFile.collection_id)
                .where(
                    Collection.user_id == user_id,
                    Collection.media_id.in_(chunk),
                    CollectionFile.source_id.isnot(None),
                )
            )
            for source_id, source_type, media_id in files_result.all():
                source_ids_map.setdefault((source_type, media_id), []).append(source_id)

        import httpx as _httpx
        sem = asyncio.Semaphore(20)

        async def _guarded(coro):
            async with sem:
                return await coro

        nuvio_watched_items: list[dict] | None = None
        nuvio_api_key = (
            await _get_effective_tmdb_key(db, settings)
            if any(conn.type == "nuvio" and conn.push_watched for conn in push_candidates)
            else None
        )

        async def _push_to_nuvio(conn: MediaServerConnection, items: list[dict]) -> bool:
            session = await nuvio.push_watched_items(
                conn.url,
                conn.token,
                _nuvio_profile_id(conn),
                items,
            )
            conn.token = session.refresh_token
            return True

        for conn in push_candidates:
            if conn.type == "nuvio":
                if conn.push_watched:
                    if nuvio_watched_items is None:
                        nuvio_watched_items = await _build_nuvio_watched_items(
                            db,
                            user_id,
                            new_watched_ids,
                            api_key=nuvio_api_key,
                        )
                    if nuvio_watched_items:
                        push_tasks.append(_guarded(_push_to_nuvio(conn, nuvio_watched_items)))
                continue
            conn_source = CollectionSource(conn.type)
            if conn.push_watched:
                for mid in new_watched_ids:
                    for sid in source_ids_map.get((conn_source, mid), []):
                        if conn.type == "plex":
                            push_tasks.append(_guarded(plex.mark_watched(conn.url, conn.token, sid)))
                        elif conn.type == "jellyfin":
                            push_tasks.append(_guarded(jellyfin.mark_watched(conn.url, conn.token, conn.server_user_id, sid)))
                        elif conn.type == "emby":
                            push_tasks.append(_guarded(emby.mark_watched(conn.url, conn.token, conn.server_user_id, sid)))
            if conn.push_ratings:
                for (mid, season_number), rating in server_rating_changes.items():
                    media = media_by_id.get(mid)
                    if season_number is not None:
                        if conn.type == "plex" and media and media.tmdb_id:
                            async def _set_plex_season_rating(
                                target_conn: MediaServerConnection = conn,
                                target_media: Media = media,
                                target_season: int = season_number,
                                target_rating: float = rating,
                            ) -> bool:
                                rating_key = await plex.resolve_season_rating_key(
                                    target_conn.url,
                                    target_conn.token,
                                    target_media.tmdb_id,
                                    target_season,
                                )
                                if not rating_key:
                                    return False
                                return await plex.set_rating(
                                    target_conn.url,
                                    target_conn.token,
                                    rating_key,
                                    target_rating,
                                )

                            push_tasks.append(_guarded(_set_plex_season_rating()))
                        continue
                    for sid in source_ids_map.get((conn_source, mid), []):
                        if conn.type == "plex":
                            push_tasks.append(_guarded(plex.set_rating(conn.url, conn.token, sid, rating)))
                        elif conn.type == "jellyfin":
                            push_tasks.append(_guarded(jellyfin.set_rating(conn.url, conn.token, conn.server_user_id, sid, rating)))
                        elif conn.type == "emby":
                            push_tasks.append(_guarded(emby.set_rating(conn.url, conn.token, conn.server_user_id, sid, rating)))

    season_tmdb_ids: dict[RatingKey, int] = {}
    # ── Trakt fan-out ────────────────────────────────────────────────────────
    push_trakt_watched = settings and exclude_cloud_source != CollectionSource.trakt and settings.trakt_push_watched and settings.trakt_access_token and settings.trakt_client_id
    push_trakt_ratings = settings and exclude_cloud_source != CollectionSource.trakt and settings.trakt_push_ratings and settings.trakt_access_token and settings.trakt_client_id

    if (push_trakt_watched or push_trakt_ratings) and all_changed_ids:
        media_items = list(media_by_id.values())

        # Load shows for episode tmdb_id lookups
        show_ids = {m.show_id for m in media_items if m.show_id}
        shows_by_id: dict[int, "Show"] = {}
        if show_ids:
            shows_list = await _select_in_chunks(
                db,
                lambda chunk: select(Show).where(Show.id.in_(chunk)),
                list(show_ids),
            )
            shows_by_id = {s.id: s for s in shows_list}

        trakt_history_movies: list[int] = []
        trakt_history_episodes: list[tuple[int, int, int]] = []
        if push_trakt_watched:
            for mid in new_watched_ids:
                media = media_by_id.get(mid)
                if not media or not media.tmdb_id:
                    continue
                if media.media_type == MediaType.movie:
                    trakt_history_movies.append(media.tmdb_id)
                elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                    show = shows_by_id.get(media.show_id)
                    if show and show.tmdb_id:
                        trakt_history_episodes.append((show.tmdb_id, media.season_number, media.episode_number))

        if trakt_history_movies or trakt_history_episodes:
            push_tasks.append(trakt_client.add_to_history_batch(
                settings.trakt_client_id, settings.trakt_access_token,
                trakt_history_movies, trakt_history_episodes,
            ))

        trakt_movie_ratings: list[tuple[int, float]] = []
        trakt_show_ratings: list[tuple[int, float]] = []
        trakt_season_ratings: list[tuple[int, float]] = []
        if push_trakt_ratings:
            all_rating_keys = set(new_ratings) | removed_ratings
            season_tmdb_ids = await _resolve_tmdb_season_ids(
                media_by_id,
                all_rating_keys,
                await _get_effective_tmdb_key(db, settings),
            )
            for key, rating in new_ratings.items():
                mid, season_number = key
                media = media_by_id.get(mid)
                if not media or not media.tmdb_id:
                    continue
                if season_number is not None:
                    if season_tmdb_id := season_tmdb_ids.get(key):
                        trakt_season_ratings.append((season_tmdb_id, rating))
                elif media.media_type == MediaType.movie:
                    trakt_movie_ratings.append((media.tmdb_id, rating))
                elif media.media_type == MediaType.series:
                    trakt_show_ratings.append((media.tmdb_id, rating))

        if trakt_movie_ratings or trakt_show_ratings or trakt_season_ratings:
            push_tasks.append(
                trakt_client.set_ratings_batch(
                    settings.trakt_client_id,
                    settings.trakt_access_token,
                    trakt_movie_ratings,
                    trakt_show_ratings,
                    trakt_season_ratings,
                )
            )

        if push_trakt_ratings:
            removed_trakt_movies: list[int] = []
            removed_trakt_shows: list[int] = []
            removed_trakt_seasons: list[int] = []
            for key in removed_ratings:
                media_id, season_number = key
                media = media_by_id.get(media_id)
                if not media or not media.tmdb_id:
                    continue
                if season_number is not None:
                    if season_tmdb_id := season_tmdb_ids.get(key):
                        removed_trakt_seasons.append(season_tmdb_id)
                elif media.media_type == MediaType.movie:
                    removed_trakt_movies.append(media.tmdb_id)
                elif media.media_type == MediaType.series:
                    removed_trakt_shows.append(media.tmdb_id)
            if removed_trakt_movies or removed_trakt_shows or removed_trakt_seasons:
                push_tasks.append(
                    trakt_client.remove_ratings_batch(
                        settings.trakt_client_id,
                        settings.trakt_access_token,
                        removed_trakt_movies,
                        removed_trakt_shows,
                        removed_trakt_seasons,
                    )
                )

    # ── MDBList fan-out ──────────────────────────────────────────────────────
    push_mdblist_watched = settings and exclude_cloud_source != CollectionSource.mdblist and settings.mdblist_push_watched and settings.mdblist_api_key
    push_mdblist_ratings = settings and exclude_cloud_source != CollectionSource.mdblist and settings.mdblist_push_ratings and settings.mdblist_api_key

    if (push_mdblist_watched or push_mdblist_ratings) and all_changed_ids:
        from core import mdblist as mdblist_client
        from routers.mdblist import _empty_payload, _payload_item, _rating_removal_item

        mdblist_media_by_id = media_by_id

        if push_mdblist_watched:
            watched_at_by_media = await _latest_watched_at(db, user_id, list(new_watched_ids))

            watched_payload = _empty_payload()
            for media_id in new_watched_ids:
                media = mdblist_media_by_id.get(media_id)
                item = _payload_item(media, watched_at=watched_at_by_media.get(media_id, datetime.utcnow())) if media else None
                if item:
                    watched_payload[item[0]].append(item[1])
            push_tasks.append(mdblist_client.push_watched(settings.mdblist_api_key, watched_payload))

        if push_mdblist_ratings and new_ratings:
            rated_media_ids = list({media_id for media_id, _ in new_ratings})
            rated_at_by_key: dict[RatingKey, datetime] = {}
            for i in range(0, len(rated_media_ids), _MAX_IN_PARAMS):
                chunk = rated_media_ids[i : i + _MAX_IN_PARAMS]
                rated_at_result = await db.execute(
                    select(Rating.media_id, Rating.season_number, Rating.rated_at).where(
                        Rating.user_id == user_id,
                        Rating.media_id.in_(chunk),
                    )
                )
                rated_at_by_key.update(
                    {
                        (media_id, season_number): rated_at
                        for media_id, season_number, rated_at in rated_at_result.all()
                    }
                )
            ratings_payload = _empty_payload()
            for key, rating in new_ratings.items():
                media_id, season_number = key
                media = mdblist_media_by_id.get(media_id)
                item = (
                    _payload_item(
                        media,
                        rating=rating,
                        rated_at=rated_at_by_key.get(key),
                        season_number=season_number,
                    )
                    if media
                    else None
                )
                if item:
                    ratings_payload[item[0]].append(item[1])
            push_tasks.append(mdblist_client.push_ratings(settings.mdblist_api_key, ratings_payload))

        if push_mdblist_ratings and removed_ratings:
            removed_payload = _empty_payload()
            for media_id, season_number in removed_ratings:
                media = mdblist_media_by_id.get(media_id)
                item = (
                    _rating_removal_item(media, season_number)
                    if media
                    else None
                )
                if item:
                    removed_payload[item[0]].append(item[1])
            push_tasks.append(
                mdblist_client.remove_ratings(
                    settings.mdblist_api_key,
                    removed_payload,
                )
            )

    # ── Simkl fan-out ────────────────────────────────────────────────────────
    push_simkl_ratings = (
        settings
        and exclude_cloud_source != CollectionSource.simkl
        and settings.simkl_push_ratings
        and settings.simkl_access_token
        and settings.simkl_client_id
    )
    if push_simkl_ratings:
        from core import simkl as simkl_client

        for key, rating in new_ratings.items():
            media_id, season_number = key
            if season_number is not None:
                continue
            media = media_by_id.get(media_id)
            if not media or not media.tmdb_id:
                continue
            if media.media_type == MediaType.movie:
                push_tasks.append(
                    simkl_client.set_movie_rating(
                        settings.simkl_client_id,
                        settings.simkl_access_token,
                        media.tmdb_id,
                        rating,
                    )
                )
            elif media.media_type == MediaType.series:
                push_tasks.append(
                    simkl_client.set_show_rating(
                        settings.simkl_client_id,
                        settings.simkl_access_token,
                        media.tmdb_id,
                        rating,
                    )
                )
        for media_id, season_number in removed_ratings:
            if season_number is not None:
                continue
            media = media_by_id.get(media_id)
            if not media or not media.tmdb_id:
                continue
            if media.media_type == MediaType.movie:
                push_tasks.append(
                    simkl_client.remove_movie_rating(
                        settings.simkl_client_id,
                        settings.simkl_access_token,
                        media.tmdb_id,
                    )
                )
            elif media.media_type == MediaType.series:
                push_tasks.append(
                    simkl_client.remove_show_rating(
                        settings.simkl_client_id,
                        settings.simkl_access_token,
                        media.tmdb_id,
                    )
                )

    if push_tasks:
        target_count = len(push_candidates)
        target_count += 1 if (push_trakt_watched or push_trakt_ratings) else 0
        target_count += 1 if (push_mdblist_watched or push_mdblist_ratings) else 0
        target_count += 1 if push_simkl_ratings else 0
        print(f"  Fanning out {len(push_tasks)} changes to {target_count} other connection(s)...")
        results = await asyncio.gather(*push_tasks, return_exceptions=True)
        failed = sum(1 for r in results if isinstance(r, Exception))
        if failed:
            print(f"  {failed}/{len(push_tasks)} fan-out push tasks failed (non-fatal)")
        if any(conn.type == "nuvio" for conn in push_candidates):
            await db.commit()


async def sync_items(
    items: list,
    media_type: MediaType,
    source: CollectionSource,
    db: AsyncSession,
    stats: dict,
    user_id: int,
    job_id: int = None,
    show_map: dict = {},
    api_key: str = None,
    show_id_to_tmdb: dict = {},  # show.id → series tmdb_id, for episode enrichment
    sync_collection: bool = True,
    sync_watched: bool = True,
    sync_ratings: bool = True,
    new_watched_ids: set[int] | None = None,  # accumulated across calls; mutated in-place
    new_ratings: RatingChanges | None = None,  # accumulated across calls; mutated in-place
    connection_id: int | None = None,
) -> list[dict]:  # returns warnings
    print(f"  Syncing {len(items)} {media_type.value}s from {source.value}...")

    # ── Phase 1: Pre-load existing data (replaces all N+1 queries) ────────────

    # All existing CollectionFiles for this user+source: source_id → (CollectionFile, media_id, Media)
    files_q = await db.execute(
        select(CollectionFile, Collection.media_id, Media)
        .join(Collection, Collection.id == CollectionFile.collection_id)
        .join(Media, Media.id == Collection.media_id)
        .where(Collection.user_id == user_id, CollectionFile.source == source)
    )
    files_rows = files_q.all()
    existing_files: dict[str, tuple[CollectionFile, int, Media]] = {
        f.source_id: (f, media_id, m) for f, media_id, m in files_rows
    }
    # (media_id, source) → CollectionFile — to detect webhook-vs-sync source_id mismatches
    files_by_media_source: dict[tuple[int, CollectionSource], CollectionFile] = {
        (media_id, f.source): f for f, media_id, _ in files_rows
    }

    # All existing Collections for this user: media_id → Collection.id
    # Used to attach new CollectionFiles to existing Collections (multi-source items)
    colls_q = await db.execute(
        select(Collection.id, Collection.media_id).where(Collection.user_id == user_id)
    )
    existing_coll_by_media_id: dict[int, int] = {
        media_id: coll_id for coll_id, media_id in colls_q.all()
    }

    # All relevant media, keyed for O(1) lookup
    media_by_episode: dict[tuple, Media] = {}   # (show_id, season, ep) → Media
    media_by_tmdb: dict[tuple, Media] = {}       # (tmdb_id, media_type) → Media

    if media_type == MediaType.episode:
        show_ids = list(set(show_map.values()))
        if show_ids:
            episodes = await _select_in_chunks(
                db,
                lambda chunk: select(Media).where(Media.media_type == MediaType.episode, Media.show_id.in_(chunk)),
                show_ids,
            )
            for m in episodes:
                media_by_episode[(m.show_id, m.season_number, m.episode_number)] = m
        # Also pre-load orphaned episode rows (show_id=None, created by webhook before first sync)
        # so they can be deduplicated by TMDB ID instead of creating a second row.
        ep_tmdb_ids: set[int] = set()
        for item in items:
            tid = (
                get_jellyfin_tmdb_id(item.get("ProviderIds", {}))
                if source in _MEDIA_BROWSER_ITEM_SOURCES
                else plex.extract_tmdb_id(item.get("Guid", []))
            )
            if tid:
                ep_tmdb_ids.add(tid)
        if ep_tmdb_ids:
            orphans = await _select_in_chunks(
                db,
                lambda chunk: select(Media).where(
                    Media.media_type == MediaType.episode,
                    Media.tmdb_id.in_(chunk),
                    Media.show_id.is_(None),
                ),
                list(ep_tmdb_ids),
            )
            for m in orphans:
                media_by_tmdb[(m.tmdb_id, m.media_type)] = m
    else:
        tmdb_ids: set[int] = set()
        for item in items:
            tid = (
                get_jellyfin_tmdb_id(item.get("ProviderIds", {}))
                if source in _MEDIA_BROWSER_ITEM_SOURCES
                else plex.extract_tmdb_id(item.get("Guid", []))
            )
            if tid:
                tmdb_ids.add(tid)
        if tmdb_ids:
            medias = await _select_in_chunks(
                db,
                lambda chunk: select(Media).where(Media.media_type == media_type, Media.tmdb_id.in_(chunk)),
                list(tmdb_ids),
            )
            for m in medias:
                media_by_tmdb[(m.tmdb_id, m.media_type)] = m

    # Reverse lookup: media.id → Media object (for healing unenriched items in skipped branch)
    media_by_id: dict[int, Media] = {m.id: m for _, _, m in files_rows}
    for m in list(media_by_episode.values()) + list(media_by_tmdb.values()):
        media_by_id[m.id] = m

    # Existing watch event media_ids (only need the int, not the ORM object)
    we_res = await db.execute(select(WatchEvent.media_id).where(WatchEvent.user_id == user_id))
    existing_watched: set[int] = {row[0] for row in we_res}

    # Existing ratings: media_id → Rating
    rat_res = await db.execute(select(Rating).where(Rating.user_id == user_id))
    existing_ratings: dict[int, Rating] = {r.media_id: r for r in rat_res.scalars()}

    # ── Phase 2: Main sync loop (no N+1 queries, savepoints for error isolation) ──
    new_media_for_enrichment: list[tuple] = []  # (Media, series_tmdb_id | None)
    skipped_warnings: list[dict] = []

    for i, item in enumerate(items):
        new_media: Media | None = None
        try:
            async with db.begin_nested():
                if source in _MEDIA_BROWSER_ITEM_SOURCES:
                    source_id = str(item.get("Id"))
                    quality = extract_jellyfin_quality(item)
                    tmdb_id = get_jellyfin_tmdb_id(item.get("ProviderIds", {}))
                    parent_id = item.get("SeriesId")
                    name = item.get("Name")
                    season_num = item.get("ParentIndexNumber")
                    episode_num = item.get("IndexNumber")
                else:  # Plex
                    source_id = str(item.get("ratingKey"))
                    quality = plex.extract_quality(item.get("Media", []))
                    tmdb_id = plex.extract_tmdb_id(item.get("Guid", []))
                    parent_id = item.get("grandparentRatingKey")
                    name = item.get("title")
                    season_num = item.get("parentIndex")
                    episode_num = item.get("index")

                file_entry = existing_files.get(source_id)
                media_id_for_watch: int | None = None

                # Detect re-match: same Plex ratingKey but TMDB ID changed.
                # Evict the stale CollectionFile so the item is re-processed below.
                if file_entry and tmdb_id and sync_collection:
                    _, _existing_media_id, _existing_media = file_entry
                    if _existing_media.tmdb_id is not None and _existing_media.tmdb_id != tmdb_id:
                        stale_file = file_entry[0]
                        stale_collection_id = stale_file.collection_id
                        await db.delete(stale_file)
                        await db.flush()
                        remaining_q = await db.execute(
                            select(func.count(CollectionFile.id)).where(
                                CollectionFile.collection_id == stale_collection_id
                            )
                        )
                        if remaining_q.scalar() == 0:
                            stale_coll = await db.get(Collection, stale_collection_id)
                            if stale_coll:
                                await db.delete(stale_coll)
                                existing_coll_by_media_id.pop(_existing_media_id, None)
                        existing_files.pop(source_id, None)
                        files_by_media_source.pop((_existing_media_id, source), None)
                        file_entry = None

                if file_entry:
                    existing_file, existing_media_id, existing_media_obj = file_entry
                    if sync_collection:
                        # Update quality metadata in-place on the CollectionFile.
                        # Never overwrite language lists with empty — bulk endpoints (e.g. Plex
                        # /library/sections/all) often omit Part.Stream data, so an empty result
                        # means "not available here", not "no languages".
                        existing_file.resolution = quality.get("resolution")
                        existing_file.video_codec = quality.get("video_codec")
                        existing_file.audio_codec = quality.get("audio_codec")
                        existing_file.audio_channels = quality.get("audio_channels")
                        if quality.get("audio_languages"):
                            existing_file.audio_languages = quality["audio_languages"]
                        if quality.get("subtitle_languages"):
                            existing_file.subtitle_languages = quality["subtitle_languages"]
                        existing_file.file_path = quality.get("file_path")
                        if connection_id is not None:
                            existing_file.connection_id = connection_id
                    stats["skipped"] += 1
                    media_id_for_watch = existing_media_id

                    # Heal missing TMDB ID for movies
                    if media_type == MediaType.movie and existing_media_obj.tmdb_id is None and tmdb_id is not None:
                        existing_media_obj.tmdb_id = tmdb_id
                        if not any(m is existing_media_obj for m, _ in new_media_for_enrichment):
                            new_media_for_enrichment.append((existing_media_obj, None))

                    # Heal unenriched episodes: webhook may have created a Media row
                    # without show_id/poster_path before the first sync ran.
                    if media_type == MediaType.episode:
                        show_id = show_map.get(str(parent_id)) if parent_id else None
                        if show_id:
                            if existing_media_obj and (
                                existing_media_obj.show_id is None
                                or (existing_media_obj.poster_path is None and not existing_media_obj.tmdb_data)
                            ):
                                ep_series_tmdb_id = show_id_to_tmdb.get(show_id)
                                if ep_series_tmdb_id:
                                    existing_media_obj.show_id = show_id
                                    # Also fill in season/episode numbers if the webhook
                                    # created the row without them — required for enrichment.
                                    if existing_media_obj.season_number is None and season_num is not None:
                                        existing_media_obj.season_number = season_num
                                    if existing_media_obj.episode_number is None and episode_num is not None:
                                        existing_media_obj.episode_number = episode_num
                                    if not any(m is existing_media_obj for m, _ in new_media_for_enrichment):
                                        new_media_for_enrichment.append((existing_media_obj, ep_series_tmdb_id))
                        else:
                            # Heal missing show_title tag on existing stub episodes (synced before
                            # stub-tagging was introduced). Backfill so match-unmatched-show can find them.
                            if (
                                existing_media_obj.tmdb_id is None
                                and existing_media_obj.show_id is None
                                and not (existing_media_obj.tmdb_data or {}).get("show_title")
                            ):
                                _series_name = (
                                    item.get("SeriesName") if source in _MEDIA_BROWSER_ITEM_SOURCES
                                    else item.get("grandparentTitle")
                                )
                                if _series_name:
                                    existing_media_obj.tmdb_data = {
                                        **(existing_media_obj.tmdb_data or {}),
                                        "show_title": _series_name,
                                    }
                else:
                    show_id = show_map.get(str(parent_id)) if media_type == MediaType.episode else None

                    # For Jellyfin/Emby episodes whose metadata scraping failed: the item title
                    # is often the raw filename (e.g. "Show.Name.S02E01"). Try to salvage the
                    # season/episode numbers from the filename so the item can be stored and
                    # later enriched (or generate a Remap-capable enrichment warning) instead of
                    # being silently skipped as unmatched.
                    if (media_type == MediaType.episode and show_id and not tmdb_id
                            and (season_num is None or episode_num is None)):
                        _m = re.search(r'[Ss](\d+)[Ee](\d+)', name or '')
                        if _m:
                            if season_num is None:
                                season_num = int(_m.group(1))
                            if episode_num is None:
                                episode_num = int(_m.group(2))

                    # Look up existing media from pre-loaded dicts (O(1), no DB query)
                    if media_type == MediaType.episode and show_id:
                        media = media_by_episode.get((show_id, season_num, episode_num))
                        if not media and tmdb_id:
                            # Fallback: catch orphaned rows created by webhook without show_id
                            media = media_by_tmdb.get((tmdb_id, media_type))
                            if media:
                                # Backfill missing show_id so future lookups work correctly
                                media.show_id = show_id
                                media_by_episode[(show_id, season_num, episode_num)] = media
                    elif tmdb_id:
                        media = media_by_tmdb.get((tmdb_id, media_type))
                    else:
                        media = None

                    if media and (media.id, source) in files_by_media_source:
                        # Media has a CollectionFile for this source but a different source_id
                        # (e.g., webhook ratingKey differs from sync ratingKey for the same item).
                        # Update the existing CollectionFile in-place instead of inserting a duplicate.
                        if sync_collection:
                            existing_alt_file = files_by_media_source[(media.id, source)]
                            existing_alt_file.source_id = source_id
                            existing_alt_file.resolution = quality.get("resolution")
                            existing_alt_file.video_codec = quality.get("video_codec")
                            existing_alt_file.audio_codec = quality.get("audio_codec")
                            existing_alt_file.audio_channels = quality.get("audio_channels")
                            if quality.get("audio_languages"):
                                existing_alt_file.audio_languages = quality["audio_languages"]
                            if quality.get("subtitle_languages"):
                                existing_alt_file.subtitle_languages = quality["subtitle_languages"]
                            existing_alt_file.file_path = quality.get("file_path")
                            if connection_id is not None:
                                existing_alt_file.connection_id = connection_id
                            # Keep in-memory maps consistent
                            old_source_id = existing_alt_file.source_id
                            existing_files.pop(old_source_id, None)
                            existing_files[source_id] = (existing_alt_file, media.id, tmdb_id)
                            files_by_media_source[(media.id, source)] = existing_alt_file
                        stats["skipped"] += 1
                        media_id_for_watch = media.id
                    else:
                        if not media:
                            can_store_stub = False
                            series_name: str | None = None
                            plex_guids: list[str] = []
                            if not tmdb_id:
                                # TV episodes belonging to a known show can still be tracked and
                                # enriched later even without an individual episode TMDB ID (e.g.
                                # Jellyfin hasn't finished fetching episode metadata yet).
                                # Everything else (movies, episodes without show context) is skipped.
                                series_name = (
                                    item.get("SeriesName") if source in _MEDIA_BROWSER_ITEM_SOURCES
                                    else item.get("grandparentTitle")
                                ) if media_type == MediaType.episode else None

                                # Episodes with no TMDB show match but with a known series name,
                                # season, and episode number are stored as stubs so the user can
                                # later match them to TVDB from the Settings warnings panel.
                                # Movies with no TMDB match are stored as stubs so the user can
                                # later match them from the Settings warnings panel.
                                can_store_stub = (
                                    media_type == MediaType.episode
                                    and series_name
                                    and season_num is not None
                                    and episode_num is not None
                                ) or (
                                    media_type == MediaType.movie
                                    and bool(name)
                                )

                                if not (show_id or can_store_stub):
                                    skipped_warnings.append({
                                        "title": name,
                                        "media_type": media_type.value,
                                        "source_id": source_id,
                                        **({"series_name": series_name} if series_name else {}),
                                        "reason": "Unmatched on source — no TMDB ID available",
                                    })
                                    stats["skipped"] += 1
                                    raise Exception("Skip this item (unmatched)") # Triggers rollback of the nested transaction

                                # Stub episode/movie: add a warning (for the settings panel) and let the
                                # Media row be created below so the user can match it later.
                                if can_store_stub and not show_id:
                                    plex_guids = [
                                        g["id"] for g in (item.get("Guid") or [])
                                        if isinstance(g, dict) and g.get("id")
                                    ]
                                    skipped_warnings.append({
                                        "title": name,
                                        "media_type": media_type.value,
                                        "source_id": source_id,
                                        **({"series_name": series_name} if series_name else {}),
                                        **({"plex_guids": plex_guids} if plex_guids else {}),
                                        "reason": "Unmatched on source — no TMDB ID available",
                                    })

                            media = Media(
                                tmdb_id=tmdb_id,
                                media_type=media_type,
                                title=name,
                                show_id=show_id,
                                season_number=season_num,
                                episode_number=episode_num,
                            )
                            db.add(media)
                            await db.flush()  # Get generated ID
                            new_media = media  # Cache updated after savepoint commits below

                            # Tag stub episodes so the match-unmatched-show endpoint can find them
                            if can_store_stub and not show_id and media.tmdb_data is None and media_type == MediaType.episode:
                                media.tmdb_data = {
                                    "show_title": series_name,
                                    **({"plex_guids": plex_guids} if plex_guids else {}),
                                }

                            ep_series_tmdb_id = show_id_to_tmdb.get(show_id) if show_id else None
                            if tmdb_id or ep_series_tmdb_id:
                                new_media_for_enrichment.append((media, ep_series_tmdb_id))

                        if sync_collection:
                            coll_id = existing_coll_by_media_id.get(media.id)
                            if coll_id is None:
                                # Upsert: ON CONFLICT DO NOTHING guards against races
                                # between concurrent webhooks / savepoint rollbacks that
                                # desynchronise the in-memory dict from the DB.
                                coll_stmt = insert(Collection).values(user_id=user_id, media_id=media.id)
                                coll_stmt = coll_stmt.on_conflict_do_nothing(constraint="uq_collection_user_media")
                                await db.execute(coll_stmt)
                                await db.flush()
                                coll_result = await db.execute(
                                    select(Collection.id).where(
                                        Collection.user_id == user_id,
                                        Collection.media_id == media.id,
                                    )
                                )
                                coll_id = coll_result.scalar_one()
                                existing_coll_by_media_id[media.id] = coll_id
                                stat_key = "movies" if media_type == MediaType.movie else "series" if media_type == MediaType.series else "episodes"
                                stats[stat_key] = stats.get(stat_key, 0) + 1
                            # else: collection already exists from another source — just add the file
                            db.add(CollectionFile(
                                collection_id=coll_id,
                                connection_id=connection_id,
                                source=source,
                                source_id=source_id,
                                file_path=quality.get("file_path"),
                                resolution=quality.get("resolution"),
                                video_codec=quality.get("video_codec"),
                                audio_codec=quality.get("audio_codec"),
                                audio_channels=quality.get("audio_channels"),
                                audio_languages=quality.get("audio_languages"),
                                subtitle_languages=quality.get("subtitle_languages"),
                            ))
                        media_id_for_watch = media.id

                if media_id_for_watch is not None:
                    watch_state = extract_watch_state(item, source)
                    if sync_watched and (watch_state["completed"] or watch_state["play_count"] > 0) and media_id_for_watch not in existing_watched:
                        db.add(WatchEvent(
                            user_id=user_id,
                            media_id=media_id_for_watch,
                            watched_at=watch_state["last_played"] or datetime.now(timezone.utc).replace(tzinfo=None),
                            completed=watch_state["completed"],
                            play_count=max(1, watch_state["play_count"]),
                            progress_percent=1.0 if watch_state["completed"] else 0.0,
                        ))
                        existing_watched.add(media_id_for_watch)
                        if new_watched_ids is not None:
                            new_watched_ids.add(media_id_for_watch)

                    if sync_ratings and watch_state["user_rating"] is not None:
                        existing_r = existing_ratings.get(media_id_for_watch)
                        if existing_r:
                            existing_r.rating = watch_state["user_rating"]
                        else:
                            new_r = Rating(user_id=user_id, media_id=media_id_for_watch, rating=watch_state["user_rating"])
                            db.add(new_r)
                            existing_ratings[media_id_for_watch] = new_r
                        if new_ratings is not None:
                            new_ratings[(media_id_for_watch, None)] = watch_state["user_rating"]

            # Savepoint committed — update pre-loaded caches so duplicates within the
            # same sync batch reuse the newly created media instead of creating another.
            if new_media:
                if media_type == MediaType.episode and new_media.show_id:
                    media_by_episode[(new_media.show_id, new_media.season_number, new_media.episode_number)] = new_media
                elif new_media.tmdb_id:
                    media_by_tmdb[(new_media.tmdb_id, new_media.media_type)] = new_media

        except Exception as e:
            if str(e) == "Skip this item (unmatched)":
                continue
            # Savepoint already rolled back — remove the enrichment entry we may have queued
            if new_media and new_media_for_enrichment and new_media_for_enrichment[-1][0] is new_media:
                new_media_for_enrichment.pop()
            stats["errors"] += 1
            print(f"    Error syncing item {i}: {e}")

        if (i + 1) % BATCH_SIZE == 0:
            await db.commit()
            if job_id:
                await db.execute(
                    update(SyncJob)
                    .where(SyncJob.id == job_id)
                    .values(processed_items=SyncJob.processed_items + BATCH_SIZE, updated_at=func.now())
                )
                await db.commit()
            print(f"    Processed {i+1}/{len(items)} items...")

    await db.commit()
    processed_remainder = len(items) % BATCH_SIZE
    if job_id and processed_remainder > 0:
        await db.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id)
            .values(processed_items=SyncJob.processed_items + processed_remainder, updated_at=func.now())
        )
        await db.commit()

    # ── Phase 3: Batch enrich newly created media ─────────────────────────────
    warnings: list[dict] = []
    if new_media_for_enrichment:
        unique_seasons = len({(stid, m.season_number) for m, stid in new_media_for_enrichment if m.media_type == MediaType.episode and stid})
        print(f"  Enriching {len(new_media_for_enrichment)} new items ({unique_seasons} unique seasons)...")

        # Build series_tmdb_id → source title map so warnings can name the show
        series_title_map: dict[int, str] = {}
        if media_type == MediaType.episode:
            for item in items:
                if source in _MEDIA_BROWSER_ITEM_SOURCES:
                    parent_id = str(item.get("SeriesId", ""))
                    title = item.get("SeriesName")
                else:
                    parent_id = str(item.get("grandparentRatingKey", ""))
                    title = item.get("grandparentTitle")
                if parent_id and title:
                    show_id = show_map.get(parent_id)
                    if show_id:
                        series_tmdb_id = show_id_to_tmdb.get(show_id)
                        if series_tmdb_id:
                            series_title_map[series_tmdb_id] = title

        warnings = await batch_enrich_items(new_media_for_enrichment, api_key=api_key, show_title_map=series_title_map)
        await db.commit()

    all_warnings = skipped_warnings + warnings
    print(f"  Finished syncing {media_type.value}s. Stats: {stats}")
    return all_warnings


async def run_jellyfin_sync(user_id: int, job_id: int, movie_limit: int, show_limit: int, connection_id: int | None = None):
    async with _sync_semaphore:
        await _run_jellyfin_sync(user_id, job_id, movie_limit, show_limit, connection_id)


async def _run_jellyfin_sync(user_id: int, job_id: int, movie_limit: int, show_limit: int, connection_id: int | None = None):
    print(f"Starting Jellyfin sync for user {user_id}, job {job_id}")
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running, processed_items=0, total_items=0))
            await db.commit()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            tmdb_api_key = await _get_effective_tmdb_key(db, settings)

            # Load the specific connection (or oldest jellyfin connection for this user)
            conn_q = select(MediaServerConnection).where(
                MediaServerConnection.user_id == user_id,
                MediaServerConnection.type == "jellyfin",
            )
            if connection_id:
                conn_q = conn_q.where(MediaServerConnection.id == connection_id)
            else:
                conn_q = conn_q.order_by(MediaServerConnection.id.asc()).limit(1)
            conn_result = await db.execute(conn_q)
            conn = conn_result.scalar_one_or_none()

            if not conn or not tmdb_api_key:
                err = "Missing Jellyfin connection or TMDB API key"
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                await db.commit()
                return

            j_url, j_token, j_user = conn.url, conn.token, conn.server_user_id

            print(f"  Fetching libraries from {j_url}")
            libraries = await jellyfin.get_libraries(j_url, j_token, j_user)

            sel_result = await db.execute(
                select(JellyfinLibrarySelection).where(JellyfinLibrarySelection.connection_id == conn.id)
            )
            selected_ids = {row.library_id for row in sel_result.scalars().all()}
            if selected_ids:
                libraries = [lib for lib in libraries if lib.get("Id") in selected_ids]

            print(f"  Found {len(libraries)} libraries to sync")
            stats = {"movies": 0, "episodes": 0, "skipped": 0, "errors": 0}
            all_warnings: list[dict] = []
            total_discovered = 0
            _new_watched: set[int] = set()
            _new_ratings: RatingChanges = {}

            for lib in libraries:
                lib_type = (lib.get("CollectionType") or "").lower()
                lib_id = lib.get("Id")
                lib_name = lib.get("Name")
                print(f"  Processing library: {lib_name} ({lib_type})")

                if lib_type == "movies":
                    items = await jellyfin.get_movies(lib_id, j_url, j_token, j_user)

                    if movie_limit:
                        items = items[:movie_limit]

                    movies_without_tmdb = [
                        m for m in items
                        if not get_jellyfin_tmdb_id(m.get("ProviderIds", {}))
                        and (m.get("ProviderIds", {}).get("Imdb") or m.get("Name"))
                    ]
                    if movies_without_tmdb:
                        print(f"    Resolving {len(movies_without_tmdb)} movies via IMDb/title fallback...")
                        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

                        async def resolve_movie_tmdb_id(m: dict) -> None:
                            async with semaphore:
                                pids = m.get("ProviderIds", {})
                                imdb_id = pids.get("Imdb") or pids.get("imdb")
                                try:
                                    if imdb_id:
                                        res = await tmdb.find_by_external_id(imdb_id, "imdb_id", api_key=tmdb_api_key)
                                        if res.get("movie_results"):
                                            tid = res["movie_results"][0]["id"]
                                            m.setdefault("ProviderIds", {})["Tmdb"] = str(tid)
                                            return
                                    title = m.get("Name")
                                    year = m.get("ProductionYear")
                                    if title:
                                        res = await tmdb.search_movies(title, year=year, api_key=tmdb_api_key)
                                        if res.get("results"):
                                            best = res["results"][0]
                                            for r in res["results"]:
                                                if r.get("title", "").lower() == title.lower():
                                                    best = r
                                                    break
                                            tid = best["id"]
                                            m.setdefault("ProviderIds", {})["Tmdb"] = str(tid)
                                except Exception as e:
                                    print(f"    Could not resolve movie '{m.get('Name')}': {e}")

                        await asyncio.gather(*[resolve_movie_tmdb_id(m) for m in movies_without_tmdb])

                    total_discovered += len(items)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    w = await sync_items(items, MediaType.movie, CollectionSource.jellyfin, db, stats, user_id, job_id, api_key=tmdb_api_key,
                        sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                        new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id)
                    all_warnings.extend(w)

                elif lib_type in ("tvshows", "tv"):
                    shows = await jellyfin.get_shows(lib_id, j_url, j_token, j_user)
                    if show_limit:
                        shows = shows[:show_limit]

                    series_tmdb_map = {
                        s.get("Id"): get_jellyfin_tmdb_id(s.get("ProviderIds", {}))
                        for s in shows if get_jellyfin_tmdb_id(s.get("ProviderIds", {}))
                    }

                    total_discovered += len(series_tmdb_map)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    print(f"    Mapping {len(series_tmdb_map)} shows to TMDB...")
                    show_map, show_id_to_tmdb = await sync_shows_batch(series_tmdb_map, db, api_key=tmdb_api_key)
                    unmatched_shows = [s for s in shows if str(s.get("Id")) not in show_map]
                    for s in unmatched_shows:
                        all_warnings.append({
                            "title": s.get("Name"),
                            "media_type": "series",
                            "source_id": str(s.get("Id")),
                            "reason": "Unmatched on source — no TMDB ID available for the series",
                        })

                    items = await jellyfin.get_episodes(lib_id, j_url, j_token, j_user)
                    filtered_episodes = [e for e in items if str(e.get("SeriesId")) in show_map]
                    unmatched_series_ids = {str(s.get("Id")) for s in shows if str(s.get("Id")) not in show_map}
                    unmatched_series_episodes = [e for e in items if str(e.get("SeriesId")) in unmatched_series_ids]

                    total_discovered = total_discovered - len(series_tmdb_map) + len(filtered_episodes) + len(unmatched_series_episodes)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    w = await sync_items(
                        filtered_episodes, MediaType.episode, CollectionSource.jellyfin,
                        db, stats, user_id, job_id, show_map,
                        api_key=tmdb_api_key, show_id_to_tmdb=show_id_to_tmdb,
                        sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                        new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id,
                    )
                    all_warnings.extend(w)

                    if unmatched_series_episodes:
                        w = await sync_items(
                            unmatched_series_episodes, MediaType.episode, CollectionSource.jellyfin,
                            db, stats, user_id, job_id, {},
                            api_key=tmdb_api_key, show_id_to_tmdb={},
                            sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                            new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id,
                        )
                        all_warnings.extend(w)

            print(f"Jellyfin sync job {job_id} completed. Stats: {stats}")
            await _fan_out_changes_to_other_connections(db, user_id, conn.id, _new_watched, _new_ratings, settings=settings)
            all_warnings = await _stamp_matched_show_warnings(db, user_id, all_warnings)
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats=stats, warnings=all_warnings or None, updated_at=func.now()))
            await db.commit()
            asyncio.create_task(pre_cache_all_collected_bg())
        except Exception as e:
            print(f"Jellyfin sync job {job_id} failed: {e}")
            import traceback
            traceback.print_exc()
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=str(e)[:900]))
            await db.commit()


async def run_emby_sync(user_id: int, job_id: int, movie_limit: int, show_limit: int, connection_id: int | None = None):
    async with _sync_semaphore:
        await _run_emby_sync(user_id, job_id, movie_limit, show_limit, connection_id)


async def _run_emby_sync(user_id: int, job_id: int, movie_limit: int, show_limit: int, connection_id: int | None = None):
    print(f"Starting Emby sync for user {user_id}, job {job_id}")
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running, processed_items=0, total_items=0))
            await db.commit()

            if connection_id is not None:
                conn_result = await db.execute(
                    select(MediaServerConnection).where(
                        MediaServerConnection.id == connection_id,
                        MediaServerConnection.user_id == user_id,
                        MediaServerConnection.type == "emby",
                    )
                )
            else:
                conn_result = await db.execute(
                    select(MediaServerConnection).where(
                        MediaServerConnection.user_id == user_id,
                        MediaServerConnection.type == "emby",
                    ).order_by(MediaServerConnection.id.asc()).limit(1)
                )
            conn = conn_result.scalar_one_or_none()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            tmdb_api_key = await _get_effective_tmdb_key(db, settings)

            if not conn or not conn.url or not conn.token or not conn.server_user_id:
                err = "Missing Emby connection (URL, Token, or User ID)"
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                await db.commit()
                return

            e_url = conn.url
            e_token = conn.token
            e_user = conn.server_user_id

            print(f"  Fetching libraries from {e_url}")
            libraries = await emby.get_libraries(e_url, e_token, e_user)

            sel_result = await db.execute(
                select(EmbyLibrarySelection).where(EmbyLibrarySelection.connection_id == conn.id)
            )
            selected_ids = {row.library_id for row in sel_result.scalars().all()}
            if selected_ids:
                libraries = [lib for lib in libraries if lib.get("Id") in selected_ids]

            print(f"  Found {len(libraries)} libraries to sync")
            stats = {"movies": 0, "episodes": 0, "skipped": 0, "errors": 0}
            all_warnings: list[dict] = []
            total_discovered = 0
            _new_watched: set[int] = set()
            _new_ratings: RatingChanges = {}

            for lib in libraries:
                lib_type = (lib.get("CollectionType") or "").lower()
                lib_id = lib.get("Id")
                lib_name = lib.get("Name")
                print(f"  Processing library: {lib_name} ({lib_type})")

                if lib_type == "movies":
                    items = await emby.get_movies(lib_id, e_url, e_token, e_user)

                    if movie_limit:
                        items = items[:movie_limit]

                    movies_without_tmdb = [
                        m for m in items
                        if not get_jellyfin_tmdb_id(m.get("ProviderIds", {}))
                        and (m.get("ProviderIds", {}).get("Imdb") or m.get("Name"))
                    ]
                    if movies_without_tmdb:
                        print(f"    Resolving {len(movies_without_tmdb)} movies via IMDb/title fallback...")
                        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

                        async def resolve_emby_movie_tmdb_id(m: dict) -> None:
                            async with semaphore:
                                pids = m.get("ProviderIds", {})
                                imdb_id = pids.get("Imdb") or pids.get("imdb")
                                try:
                                    if imdb_id:
                                        res = await tmdb.find_by_external_id(imdb_id, "imdb_id", api_key=tmdb_api_key)
                                        if res.get("movie_results"):
                                            tid = res["movie_results"][0]["id"]
                                            m.setdefault("ProviderIds", {})["Tmdb"] = str(tid)
                                            return
                                    title = m.get("Name")
                                    year = m.get("ProductionYear")
                                    if title:
                                        res = await tmdb.search_movies(title, year=year, api_key=tmdb_api_key)
                                        if res.get("results"):
                                            best = res["results"][0]
                                            for r in res["results"]:
                                                if r.get("title", "").lower() == title.lower():
                                                    best = r
                                                    break
                                            tid = best["id"]
                                            m.setdefault("ProviderIds", {})["Tmdb"] = str(tid)
                                except Exception as e:
                                    print(f"    Could not resolve movie '{m.get('Name')}': {e}")

                        await asyncio.gather(*[resolve_emby_movie_tmdb_id(m) for m in movies_without_tmdb])

                    total_discovered += len(items)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    w = await sync_items(items, MediaType.movie, CollectionSource.emby, db, stats, user_id, job_id, api_key=tmdb_api_key,
                        sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                        new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id)
                    all_warnings.extend(w)

                elif lib_type in ("tvshows", "tv"):
                    shows = await emby.get_shows(lib_id, e_url, e_token, e_user)
                    if show_limit:
                        shows = shows[:show_limit]

                    series_tmdb_map = {
                        s.get("Id"): get_jellyfin_tmdb_id(s.get("ProviderIds", {}))
                        for s in shows if get_jellyfin_tmdb_id(s.get("ProviderIds", {}))
                    }

                    total_discovered += len(series_tmdb_map)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    print(f"    Mapping {len(series_tmdb_map)} shows to TMDB...")
                    show_map, show_id_to_tmdb = await sync_shows_batch(
                        series_tmdb_map, db, api_key=tmdb_api_key
                    )
                    unmatched_shows = [s for s in shows if str(s.get("Id")) not in show_map]
                    for s in unmatched_shows:
                        all_warnings.append({
                            "title": s.get("Name"),
                            "media_type": "series",
                            "source_id": str(s.get("Id")),
                            "reason": "Unmatched on source — no TMDB ID available for the series",
                        })

                    items = await emby.get_episodes(lib_id, e_url, e_token, e_user)
                    filtered_episodes = [e for e in items if str(e.get("SeriesId")) in show_map]
                    unmatched_series_ids = {str(s.get("Id")) for s in shows if str(s.get("Id")) not in show_map}
                    unmatched_series_episodes = [e for e in items if str(e.get("SeriesId")) in unmatched_series_ids]

                    total_discovered = total_discovered - len(series_tmdb_map) + len(filtered_episodes) + len(unmatched_series_episodes)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    w = await sync_items(
                        filtered_episodes, MediaType.episode, CollectionSource.emby,
                        db, stats, user_id, job_id, show_map,
                        api_key=tmdb_api_key, show_id_to_tmdb=show_id_to_tmdb,
                        sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                        new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id,
                    )
                    all_warnings.extend(w)

                    if unmatched_series_episodes:
                        w = await sync_items(
                            unmatched_series_episodes, MediaType.episode, CollectionSource.emby,
                            db, stats, user_id, job_id, {},
                            api_key=tmdb_api_key, show_id_to_tmdb={},
                            sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                            new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id,
                        )
                        all_warnings.extend(w)

            print(f"Emby sync job {job_id} completed. Stats: {stats}")
            await _fan_out_changes_to_other_connections(db, user_id, conn.id, _new_watched, _new_ratings, settings=settings)
            all_warnings = await _stamp_matched_show_warnings(db, user_id, all_warnings)
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats=stats, warnings=all_warnings or None, updated_at=func.now()))
            await db.commit()
            asyncio.create_task(pre_cache_all_collected_bg())
        except Exception as e:
            print(f"Emby sync job {job_id} failed: {e}")
            import traceback
            traceback.print_exc()
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=str(e)[:900]))
            await db.commit()


_BACKFILL_CHUNK = 50  # HTTP calls per chunk; commit + progress update after each

async def _backfill_plex_languages(user_id: int, connection_id: int, p_url: str, p_token: str, job_id: int | None = None) -> int:
    """Fetch full item detail from Plex for CollectionFiles that have no language data yet.

    Runs in its own DB session so the main sync connection is released before this
    long-running phase starts. Processes in chunks to avoid holding a transaction open
    across thousands of outbound HTTP calls.
    """
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        result = await db.execute(
            select(CollectionFile)
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .where(
                Collection.user_id == user_id,
                CollectionFile.source == CollectionSource.plex,
                CollectionFile.connection_id == connection_id,
                CollectionFile.source_id.isnot(None),
                (CollectionFile.audio_languages == None) | (CollectionFile.audio_languages.cast(JSONB) == cast([], JSONB)),
            )
        )
        files = result.scalars().all()
        if not files:
            return 0

        total = len(files)
        print(f"  Backfilling language data for {total} Plex file(s)...")

        if job_id is not None:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=0, total_items=total))
            await db.commit()

        sem = asyncio.Semaphore(10)

        async def _fetch_quality(cf: CollectionFile) -> tuple[int, dict]:
            async with sem:
                item = await plex.get_item(p_url, p_token, cf.source_id)
                if not item:
                    return cf.id, {}
                return cf.id, plex.extract_quality(item.get("Media", []))

        done = 0
        for chunk_start in range(0, total, _BACKFILL_CHUNK):
            chunk = files[chunk_start:chunk_start + _BACKFILL_CHUNK]
            cf_map = {cf.id: cf for cf in chunk}

            results = await asyncio.gather(*[_fetch_quality(cf) for cf in chunk], return_exceptions=True)

            for res in results:
                if isinstance(res, Exception):
                    continue
                cf_id, quality = res
                cf = cf_map.get(cf_id)
                if cf and quality:
                    if quality.get("audio_languages"):
                        cf.audio_languages = quality["audio_languages"]
                    if quality.get("subtitle_languages"):
                        cf.subtitle_languages = quality["subtitle_languages"]

            done += len(chunk)
            await db.commit()

            if job_id is not None:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=done))
                await db.commit()

        return total


async def run_plex_sync(user_id: int, job_id: int, movie_limit: int, show_limit: int, connection_id: int | None = None):
    async with _sync_semaphore:
        await _run_plex_sync(user_id, job_id, movie_limit, show_limit, connection_id)


async def _run_plex_sync(user_id: int, job_id: int, movie_limit: int, show_limit: int, connection_id: int | None = None):
    print(f"Starting Plex sync for user {user_id}, job {job_id}")
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running, processed_items=0, total_items=0))
            await db.commit()

            if connection_id is not None:
                conn_result = await db.execute(
                    select(MediaServerConnection).where(
                        MediaServerConnection.id == connection_id,
                        MediaServerConnection.user_id == user_id,
                        MediaServerConnection.type == "plex",
                    )
                )
            else:
                conn_result = await db.execute(
                    select(MediaServerConnection).where(
                        MediaServerConnection.user_id == user_id,
                        MediaServerConnection.type == "plex",
                    ).order_by(MediaServerConnection.id.asc()).limit(1)
                )
            conn = conn_result.scalar_one_or_none()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            tmdb_api_key = await _get_effective_tmdb_key(db, settings)

            if not conn or not conn.url or not conn.token:
                err = "Missing Plex connection (URL or Token)"
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                await db.commit()
                return

            p_url = conn.url
            p_token = conn.token

            print(f"  Fetching Plex libraries...")
            libraries = await plex.get_libraries(p_url, p_token)

            sel_result = await db.execute(
                select(PlexLibrarySelection).where(PlexLibrarySelection.connection_id == conn.id)
            )
            selected_keys = {row.library_key for row in sel_result.scalars().all()}
            if selected_keys:
                libraries = [lib for lib in libraries if lib.get("key") in selected_keys]

            print(f"  Found {len(libraries)} libraries to sync")
            stats = {"movies": 0, "episodes": 0, "ratings": 0, "skipped": 0, "errors": 0}
            all_warnings: list[dict] = []
            total_discovered = 0
            _new_watched: set[int] = set()
            _new_ratings: RatingChanges = {}
            ratings_result = await db.execute(
                select(Rating).where(Rating.user_id == user_id)
            )
            existing_ratings = {
                (rating.media_id, rating.season_number): rating
                for rating in ratings_result.scalars().all()
            }

            for lib in libraries:
                lib_type = lib.get("type")
                lib_key = lib.get("key")
                lib_title = lib.get("title")
                print(f"  Processing library: {lib_title} ({lib_type})")

                if lib_type == "movie":
                    items = await plex.get_movies(p_url, p_token, lib_key)
                    if movie_limit:
                        items = items[:movie_limit]

                    movies_without_tmdb = [
                        m for m in items
                        if not plex.extract_tmdb_id(m.get("Guid", []))
                        and (plex.extract_imdb_id(m.get("Guid", [])) or m.get("title"))
                    ]
                    if movies_without_tmdb:
                        print(f"    Resolving {len(movies_without_tmdb)} movies via IMDb/title fallback...")
                        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

                        async def resolve_movie_tmdb_id(m: dict) -> None:
                            async with semaphore:
                                guids = m.get("Guid", [])
                                imdb_id = plex.extract_imdb_id(guids)
                                try:
                                    if imdb_id:
                                        res = await tmdb.find_by_external_id(imdb_id, "imdb_id", api_key=tmdb_api_key)
                                        if res.get("movie_results"):
                                            tid = res["movie_results"][0]["id"]
                                            m.setdefault("Guid", []).append({"id": f"tmdb://{tid}"})
                                            return
                                    title = m.get("title")
                                    year = m.get("year")
                                    if title:
                                        res = await tmdb.search_movies(title, year=year, api_key=tmdb_api_key)
                                        if res.get("results"):
                                            best = res["results"][0]
                                            for r in res["results"]:
                                                if r.get("title", "").lower() == title.lower():
                                                    best = r
                                                    break
                                            tid = best["id"]
                                            m.setdefault("Guid", []).append({"id": f"tmdb://{tid}"})
                                except Exception as e:
                                    print(f"    Could not resolve movie '{m.get('title')}': {e}")

                        await asyncio.gather(*[resolve_movie_tmdb_id(m) for m in movies_without_tmdb])

                    total_discovered += len(items)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    w = await sync_items(items, MediaType.movie, CollectionSource.plex, db, stats, user_id, job_id, api_key=tmdb_api_key,
                        sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                        new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id)
                    all_warnings.extend(w)

                elif lib_type == "show":
                    shows = await plex.get_shows(p_url, p_token, lib_key)
                    if show_limit:
                        shows = shows[:show_limit]

                    series_tmdb_map = {
                        s.get("ratingKey"): plex.extract_tmdb_id(plex.get_guids(s))
                        for s in shows if plex.extract_tmdb_id(plex.get_guids(s))
                    }

                    shows_without_tmdb = [
                        s for s in shows
                        if s.get("ratingKey") not in series_tmdb_map
                        and (plex.extract_tvdb_id(plex.get_guids(s)) or plex.extract_imdb_id(plex.get_guids(s)))
                    ]

                    if shows_without_tmdb:
                        print(f"    Resolving {len(shows_without_tmdb)} shows via TVDB/IMDb fallback...")
                        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

                        async def resolve_show_tmdb_id(s: dict) -> None:
                            async with semaphore:
                                guids = plex.get_guids(s)
                                tvdb_id = plex.extract_tvdb_id(guids)
                                imdb_id = plex.extract_imdb_id(guids)
                                try:
                                    if tvdb_id:
                                        res = await tmdb.find_by_external_id(tvdb_id, "tvdb_id", api_key=tmdb_api_key)
                                        if res.get("tv_results"):
                                            series_tmdb_map[s["ratingKey"]] = res["tv_results"][0]["id"]
                                            return
                                    if imdb_id:
                                        res = await tmdb.find_by_external_id(imdb_id, "imdb_id", api_key=tmdb_api_key)
                                        if res.get("tv_results"):
                                            series_tmdb_map[s["ratingKey"]] = res["tv_results"][0]["id"]
                                            return
                                    title = s.get("title") or s.get("titleSort")
                                    if title:
                                        res = await tmdb.search_shows(title, api_key=tmdb_api_key)
                                        if res.get("results"):
                                            series_tmdb_map[s["ratingKey"]] = res["results"][0]["id"]
                                except Exception as e:
                                    print(f"    Could not resolve show '{s.get('title')}': {e}")

                        await asyncio.gather(*[resolve_show_tmdb_id(s) for s in shows_without_tmdb])

                    total_discovered += len(series_tmdb_map)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    print(f"    Mapping {len(series_tmdb_map)} shows to TMDB...")
                    show_map, show_id_to_tmdb = await sync_shows_batch(
                        series_tmdb_map, db, api_key=tmdb_api_key
                    )
                    print(f"    Mapped {len(show_map)}/{len(series_tmdb_map)} shows.")

                    if conn.sync_ratings:
                        seasons = await plex.get_seasons(p_url, p_token, lib_key)
                        show_titles = {
                            str(show.get("ratingKey")): str(show.get("title") or "")
                            for show in shows
                        }
                        rated_seasons = [
                            season
                            for season in seasons
                            if season.get("userRating") is not None
                        ]
                        total_discovered += len(rated_seasons)
                        for season in rated_seasons:
                            parent_key = str(season.get("parentRatingKey") or "")
                            show_id = show_map.get(parent_key)
                            show_tmdb_id = show_id_to_tmdb.get(show_id) if show_id else None
                            season_number = season.get("index")
                            if show_tmdb_id is None or season_number is None:
                                stats["skipped"] += 1
                                continue
                            try:
                                async with db.begin_nested():
                                    media = await _get_or_create_series_rating_media(
                                        db,
                                        show_tmdb_id,
                                        show_titles.get(parent_key, ""),
                                        tmdb_api_key,
                                    )
                                    key = (media.id, int(season_number))
                                    rating_value = float(season["userRating"])
                                    current = existing_ratings.get(key)
                                    if current and current.rating == rating_value:
                                        stats["skipped"] += 1
                                        continue
                                    if current:
                                        current.rating = rating_value
                                        current.rated_at = datetime.utcnow()
                                    else:
                                        current = Rating(
                                            user_id=user_id,
                                            media_id=media.id,
                                            season_number=int(season_number),
                                            rating=rating_value,
                                        )
                                        db.add(current)
                                        existing_ratings[key] = current
                                    _new_ratings[key] = rating_value
                                    stats["ratings"] += 1
                            except Exception as exc:
                                logger.warning(
                                    "Error importing Plex season rating show=%s season=%s: %s",
                                    show_tmdb_id,
                                    season_number,
                                    exc,
                                )
                                stats["errors"] += 1

                    unmatched_shows = [s for s in shows if str(s.get("ratingKey")) not in show_map]
                    for s in unmatched_shows:
                        all_warnings.append({
                            "title": s.get("title"),
                            "media_type": "series",
                            "source_id": str(s.get("ratingKey")),
                            "plex_guids": [g.get("id", "") for g in plex.get_guids(s) if isinstance(g, dict)],
                            "reason": "Unmatched on source — no TMDB ID available for the series",
                        })

                    print(f"    Fetching episodes for {lib_title}...")
                    items = await plex.get_episodes(p_url, p_token, lib_key)
                    filtered_episodes = [i for i in items if str(i.get("grandparentRatingKey")) in show_map]
                    unmatched_ratingkeys = {str(s.get("ratingKey")) for s in shows if str(s.get("ratingKey")) not in show_map}
                    unmatched_series_episodes = [i for i in items if str(i.get("grandparentRatingKey")) in unmatched_ratingkeys]

                    total_discovered = total_discovered - len(series_tmdb_map) + len(filtered_episodes) + len(unmatched_series_episodes)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_discovered))
                    await db.commit()

                    w = await sync_items(
                        filtered_episodes, MediaType.episode, CollectionSource.plex,
                        db, stats, user_id, job_id, show_map,
                        api_key=tmdb_api_key, show_id_to_tmdb=show_id_to_tmdb,
                        sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                        new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id,
                    )
                    all_warnings.extend(w)

                    if unmatched_series_episodes:
                        w = await sync_items(
                            unmatched_series_episodes, MediaType.episode, CollectionSource.plex,
                            db, stats, user_id, job_id, {},
                            api_key=tmdb_api_key, show_id_to_tmdb={},
                            sync_collection=conn.sync_collection, sync_watched=conn.sync_watched, sync_ratings=conn.sync_ratings,
                            new_watched_ids=_new_watched, new_ratings=_new_ratings, connection_id=conn.id,
                        )
                        all_warnings.extend(w)

            # ── Plex watchlist → Scrob list ──────────────────────────────────
            if conn.plex_sync_watchlist:
                from models.lists import List as ListModel, ListItem
                PLEX_WATCHLIST_SLUG = "__plex_watchlist__"
                print(f"  Fetching Plex watchlist...")
                try:
                    wl_items = await plex.get_watchlist(p_token)
                    print(f"  {len(wl_items)} items in Plex watchlist")

                    wl_result = await db.execute(
                        select(ListModel).where(
                            ListModel.user_id == user_id,
                            ListModel.trakt_slug == PLEX_WATCHLIST_SLUG,
                        )
                    )
                    watchlist = wl_result.scalar_one_or_none()
                    if not watchlist:
                        watchlist = ListModel(user_id=user_id, name="Plex - Watchlist", trakt_slug=PLEX_WATCHLIST_SLUG)
                        db.add(watchlist)
                        await db.flush()

                    existing_result = await db.execute(
                        select(ListItem.media_id).where(ListItem.list_id == watchlist.id)
                    )
                    wl_existing_ids: set[int] = {row[0] for row in existing_result}

                    # Build set of TMDB IDs currently on Plex watchlist
                    plex_tmdb_ids: set[int] = set()
                    for item in wl_items:
                        for guid in item.get("Guid", []):
                            gid = guid.get("id", "")
                            if gid.startswith("tmdb://"):
                                try:
                                    plex_tmdb_ids.add(int(gid[7:]))
                                except ValueError:
                                    pass

                    # Remove items no longer on Plex watchlist
                    if wl_existing_ids:
                        stale_result = await db.execute(
                            select(Media).where(
                                Media.id.in_(wl_existing_ids),
                                Media.tmdb_id.notin_(plex_tmdb_ids) if plex_tmdb_ids else Media.tmdb_id.isnot(None),
                            )
                        )
                        for stale in stale_result.scalars():
                            await db.execute(
                                ListItem.__table__.delete().where(
                                    ListItem.list_id == watchlist.id,
                                    ListItem.media_id == stale.id,
                                )
                            )
                            wl_existing_ids.discard(stale.id)

                    # Add new items
                    for item in wl_items:
                        item_type = item.get("type")  # "movie" or "show"
                        tmdb_id_item: int | None = None
                        for guid in item.get("Guid", []):
                            gid = guid.get("id", "")
                            if gid.startswith("tmdb://"):
                                try:
                                    tmdb_id_item = int(gid[7:])
                                except ValueError:
                                    pass
                        if not tmdb_id_item:
                            continue
                        try:
                            if item_type == "movie":
                                media_result = await db.execute(
                                    select(Media).where(Media.tmdb_id == tmdb_id_item, Media.media_type == MediaType.movie)
                                )
                                media = media_result.scalar_one_or_none()
                                if not media:
                                    d = await tmdb.get_movie(tmdb_id_item, api_key=tmdb_api_key)
                                    media = Media(
                                        tmdb_id=tmdb_id_item,
                                        media_type=MediaType.movie,
                                        title=d.get("title") or item.get("title", ""),
                                        poster_path=tmdb.poster_url(d.get("poster_path")),
                                        backdrop_path=tmdb.poster_url(d.get("backdrop_path"), size="w1280"),
                                        release_date=d.get("release_date"),
                                        tmdb_rating=d.get("vote_average"),
                                        overview=d.get("overview"),
                                        adult=d.get("adult", False),
                                    )
                                    db.add(media)
                                    await db.flush()
                            elif item_type == "show":
                                media_result = await db.execute(
                                    select(Media).where(Media.tmdb_id == tmdb_id_item, Media.media_type == MediaType.series)
                                )
                                media = media_result.scalar_one_or_none()
                                if not media:
                                    d = await tmdb.get_show(tmdb_id_item, api_key=tmdb_api_key)
                                    media = Media(
                                        tmdb_id=tmdb_id_item,
                                        media_type=MediaType.series,
                                        title=d.get("name") or item.get("title", ""),
                                        poster_path=tmdb.poster_url(d.get("poster_path")),
                                        backdrop_path=tmdb.poster_url(d.get("backdrop_path"), size="w1280"),
                                        release_date=d.get("first_air_date"),
                                        tmdb_rating=d.get("vote_average"),
                                        overview=d.get("overview"),
                                        adult=d.get("adult", False),
                                    )
                                    db.add(media)
                                    await db.flush()
                            else:
                                continue

                            if media and media.id not in wl_existing_ids:
                                db.add(ListItem(list_id=watchlist.id, media_id=media.id))
                                wl_existing_ids.add(media.id)
                        except Exception as exc:
                            print(f"  Warning: failed to sync Plex watchlist item tmdb={tmdb_id_item}: {exc}")

                    await db.commit()
                    print(f"  Plex watchlist sync complete.")
                except Exception as exc:
                    print(f"  Warning: Plex watchlist sync failed: {exc}")
                    await db.rollback()

            backfilled = await _backfill_plex_languages(user_id, conn.id, p_url, p_token, job_id)
            if backfilled:
                print(f"Plex sync job {job_id}: backfilled language data for {backfilled} file(s).")
            print(f"Plex sync job {job_id} completed. Stats: {stats}")
            await _fan_out_changes_to_other_connections(db, user_id, conn.id, _new_watched, _new_ratings, settings=settings)
            all_warnings = await _stamp_matched_show_warnings(db, user_id, all_warnings)
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats=stats, warnings=all_warnings or None, updated_at=func.now()))
            await db.commit()
            asyncio.create_task(pre_cache_all_collected_bg())
        except Exception as e:
            print(f"Plex sync job {job_id} failed: {e}")
            import traceback
            traceback.print_exc()
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=str(e)[:900]))
            await db.commit()


def _parse_nuvio_tmdb_id(content_id: object) -> int | None:
    value = str(content_id or "")
    if not value.startswith("tmdb:"):
        return None
    try:
        return int(value[5:])
    except ValueError:
        return None


async def _resolve_nuvio_tmdb_ids(
    records: list[dict],
    db: AsyncSession,
    user_id: int,
    api_key: str,
) -> dict[str, int]:
    content_types: dict[str, str] = {}
    resolved: dict[str, int] = {}
    for record in records:
        content_id = str(record.get("content_id") or "").strip()
        if not content_id:
            continue
        if tmdb_id := _parse_nuvio_tmdb_id(content_id):
            resolved[content_id] = tmdb_id
        elif re.fullmatch(r"tt\d+", content_id, flags=re.IGNORECASE):
            content_types.setdefault(content_id, str(record.get("content_type") or "").lower())

    unresolved = set(content_types) - set(resolved)
    if unresolved:
        existing_result = await db.execute(
            select(CollectionFile.source_id, Media.tmdb_id)
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .join(Media, Media.id == Collection.media_id)
            .where(
                Collection.user_id == user_id,
                CollectionFile.source == CollectionSource.nuvio,
                Media.tmdb_id.isnot(None),
            )
        )
        for source_id, tmdb_id in existing_result.all():
            parts = str(source_id).split(":")
            if len(parts) >= 2 and parts[1] in unresolved:
                resolved[parts[1]] = int(tmdb_id)
        unresolved -= set(resolved)

    semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

    async def resolve_imdb_id(content_id: str) -> None:
        async with semaphore:
            try:
                result = await tmdb.find_by_external_id(content_id, "imdb_id", api_key=api_key)
                result_key = "movie_results" if content_types[content_id] == "movie" else "tv_results"
                matches = result.get(result_key) or []
                if matches and matches[0].get("id") is not None:
                    resolved[content_id] = int(matches[0]["id"])
            except Exception as exc:
                logger.warning(
                    "Failed to resolve Nuvio IMDb ID %s through TMDB: %s",
                    content_id,
                    exc,
                )

    if unresolved:
        await asyncio.gather(*(resolve_imdb_id(content_id) for content_id in sorted(unresolved)))
        logger.info(
            "Resolved %s/%s new Nuvio IMDb IDs through TMDB",
            len(unresolved & set(resolved)),
            len(unresolved),
        )
    return resolved


def _nuvio_datetime(epoch_ms: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _normalize_nuvio_item(
    record: dict,
    profile_id: int,
    watched: bool = False,
    tmdb_id: int | None = None,
) -> tuple[MediaType, dict] | None:
    tmdb_id = tmdb_id or _parse_nuvio_tmdb_id(record.get("content_id"))
    if tmdb_id is None:
        return None

    content_type = str(record.get("content_type") or "").lower()
    season = record.get("season")
    episode = record.get("episode")
    is_episode = content_type == "series" and season is not None and episode is not None
    if content_type == "movie":
        media_type = MediaType.movie
    elif is_episode:
        media_type = MediaType.episode
    elif content_type == "series":
        media_type = MediaType.series
    else:
        return None

    content_id = str(record["content_id"])
    source_id = f"{profile_id}:{content_id}"
    if is_episode:
        source_id = f"{source_id}:s{season}e{episode}"
    last_played = _nuvio_datetime(record.get("watched_at") or record.get("last_watched"))
    title = record.get("title") or record.get("name") or content_id

    item = {
        "Id": source_id,
        "Name": title,
        "ProviderIds": {} if is_episode else {"Tmdb": str(tmdb_id)},
        "MediaStreams": [],
        "Path": None,
        "SeriesId": content_id if is_episode else None,
        "SeriesName": title if is_episode else None,
        "ParentIndexNumber": int(season) if season is not None else None,
        "IndexNumber": int(episode) if episode is not None else None,
        "UserData": {
            "Played": watched,
            "PlayCount": 1 if watched else 0,
            "LastPlayedDate": last_played.isoformat() if last_played else None,
        },
    }
    return media_type, item


async def _apply_nuvio_progress(
    db: AsyncSession,
    user_id: int,
    rows: list[dict],
    show_map: dict[str, int],
    tmdb_ids: dict[str, int],
) -> None:
    movie_tmdb_ids = {
        tmdb_id
        for row in rows
        if str(row.get("content_type") or "").lower() == "movie"
        if (tmdb_id := tmdb_ids.get(str(row.get("content_id") or ""))) is not None
    }
    movies_by_tmdb: dict[int, Media] = {}
    if movie_tmdb_ids:
        result = await db.execute(
            select(Media).where(Media.media_type == MediaType.movie, Media.tmdb_id.in_(movie_tmdb_ids))
        )
        movies_by_tmdb = {media.tmdb_id: media for media in result.scalars().all() if media.tmdb_id is not None}

    show_ids = set(show_map.values())
    episodes_by_key: dict[tuple[int, int, int], Media] = {}
    if show_ids:
        result = await db.execute(
            select(Media).where(Media.media_type == MediaType.episode, Media.show_id.in_(show_ids))
        )
        episodes_by_key = {
            (media.show_id, media.season_number, media.episode_number): media
            for media in result.scalars().all()
            if media.show_id is not None and media.season_number is not None and media.episode_number is not None
        }

    media_rows: list[tuple[dict, Media]] = []
    for row in rows:
        content_id = str(row.get("content_id") or "")
        tmdb_id = tmdb_ids.get(content_id)
        if tmdb_id is None:
            continue
        if str(row.get("content_type") or "").lower() == "movie":
            media = movies_by_tmdb.get(tmdb_id)
        else:
            season = row.get("season")
            episode = row.get("episode")
            show_id = show_map.get(content_id)
            media = (
                episodes_by_key.get((show_id, int(season), int(episode)))
                if show_id is not None and season is not None and episode is not None
                else None
            )
        if media is not None:
            media_rows.append((row, media))

    if not media_rows:
        return

    media_ids = {media.id for _, media in media_rows}
    existing_result = await db.execute(
        select(PlaybackProgress).where(
            PlaybackProgress.user_id == user_id,
            PlaybackProgress.media_id.in_(media_ids),
        )
    )
    existing = {progress.media_id: progress for progress in existing_result.scalars().all()}

    for row, media in media_rows:
        try:
            position_ms = max(0, int(row.get("position") or 0))
            duration_ms = max(0, int(row.get("duration") or 0))
        except (TypeError, ValueError):
            continue
        if duration_ms <= 0:
            continue
        progress_percent = min(1.0, position_ms / duration_ms)
        progress = existing.get(media.id)
        if 0.05 <= progress_percent < 0.90:
            updated_at = _nuvio_datetime(row.get("last_watched")) or datetime.utcnow()
            if progress:
                progress.progress_percent = progress_percent
                progress.progress_seconds = position_ms // 1000
                progress.updated_at = updated_at
            else:
                progress = PlaybackProgress(
                    user_id=user_id,
                    media_id=media.id,
                    progress_percent=progress_percent,
                    progress_seconds=position_ms // 1000,
                    updated_at=updated_at,
                )
                db.add(progress)
                existing[media.id] = progress
        elif progress:
            await db.delete(progress)
            existing.pop(media.id, None)
    await db.commit()


async def run_nuvio_sync(
    user_id: int,
    job_id: int,
    movie_limit: int,
    show_limit: int,
    connection_id: int | None = None,
):
    async with _sync_semaphore:
        await _run_nuvio_sync(user_id, job_id, movie_limit, show_limit, connection_id)


async def _run_nuvio_sync(
    user_id: int,
    job_id: int,
    movie_limit: int,
    show_limit: int,
    connection_id: int | None = None,
):
    logger.info("Starting Nuvio sync for user %s, job %s", user_id, job_id)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(status=SyncStatus.running, processed_items=0, total_items=0)
            )
            await db.commit()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            tmdb_api_key = await _get_effective_tmdb_key(db, settings)

            conn_query = select(MediaServerConnection).where(
                MediaServerConnection.user_id == user_id,
                MediaServerConnection.type == "nuvio",
            )
            if connection_id:
                conn_query = conn_query.where(MediaServerConnection.id == connection_id)
            else:
                conn_query = conn_query.order_by(MediaServerConnection.id.asc()).limit(1)
            conn_result = await db.execute(conn_query)
            conn = conn_result.scalar_one_or_none()
            if not conn or not tmdb_api_key:
                raise RuntimeError("Missing Nuvio connection or TMDB API key")

            try:
                profile_id = int(conn.server_user_id or "")
            except ValueError:
                raise RuntimeError("Invalid Nuvio profile index")
            if profile_id < 1 or profile_id > 6:
                raise RuntimeError("Invalid Nuvio profile index")

            session, data = await nuvio.pull_sync_data(conn.url, conn.token, profile_id)
            # Supabase refresh tokens rotate. Persist the replacement before doing
            # any expensive metadata work so the connection remains recoverable.
            conn.token = session.refresh_token
            await db.commit()

            library_records = data["library"] if conn.sync_collection else []
            watched_records = data["watched"] if conn.sync_watched else []
            progress_records = data["progress"] if conn.sync_playback else []
            logger.info(
                "Nuvio profile %s (index #%s): pulled %s library, %s watched, "
                "and %s progress records; enabled for this sync: %s library, "
                "%s watched, %s progress",
                conn.server_username or f"#{profile_id}",
                profile_id,
                len(data["library"]),
                len(data["watched"]),
                len(data["progress"]),
                len(library_records),
                len(watched_records),
                len(progress_records),
            )

            all_nuvio_records = [*library_records, *watched_records, *progress_records]
            tmdb_ids = await _resolve_nuvio_tmdb_ids(
                all_nuvio_records,
                db,
                user_id,
                tmdb_api_key,
            )
            normalized_library = [
                normalized
                for record in library_records
                if (
                    normalized := _normalize_nuvio_item(
                        record,
                        profile_id,
                        tmdb_id=tmdb_ids.get(str(record.get("content_id") or "")),
                    )
                )
                is not None
            ]
            normalized_watched = [
                normalized
                for record in watched_records
                if (
                    normalized := _normalize_nuvio_item(
                        record,
                        profile_id,
                        watched=True,
                        tmdb_id=tmdb_ids.get(str(record.get("content_id") or "")),
                    )
                )
                is not None
            ]
            normalized_progress = [
                normalized
                for record in progress_records
                if (
                    normalized := _normalize_nuvio_item(
                        record,
                        profile_id,
                        tmdb_id=tmdb_ids.get(str(record.get("content_id") or "")),
                    )
                )
                is not None
            ]
            skipped_nuvio_records = len(all_nuvio_records) - (
                len(normalized_library) + len(normalized_watched) + len(normalized_progress)
            )

            if movie_limit:
                normalized_library = [
                    *[entry for entry in normalized_library if entry[0] == MediaType.movie][:movie_limit],
                    *[entry for entry in normalized_library if entry[0] != MediaType.movie],
                ]
            if show_limit:
                normalized_library = [
                    *[entry for entry in normalized_library if entry[0] != MediaType.series],
                    *[entry for entry in normalized_library if entry[0] == MediaType.series][:show_limit],
                ]

            series_tmdb_map = {
                str(record.get("content_id")): tmdb_id
                for record in [*library_records, *watched_records, *progress_records]
                if str(record.get("content_type") or "").lower() == "series"
                if (tmdb_id := tmdb_ids.get(str(record.get("content_id") or ""))) is not None
            }
            if series_tmdb_map:
                show_map, show_id_to_tmdb = await sync_shows_batch(
                    series_tmdb_map,
                    db,
                    api_key=tmdb_api_key,
                )
            else:
                show_map, show_id_to_tmdb = {}, {}

            all_entries = [*normalized_library, *normalized_watched, *normalized_progress]
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(all_entries))
            )
            await db.commit()

            stats = {"movies": 0, "series": 0, "episodes": 0, "skipped": skipped_nuvio_records, "errors": 0}
            warnings: list[dict] = []
            new_watched_ids: set[int] = set()

            async def sync_group(
                entries: list[tuple[MediaType, dict]],
                media_type: MediaType,
                *,
                sync_collection: bool,
                sync_watched: bool,
            ) -> None:
                items = [item for item_type, item in entries if item_type == media_type]
                if not items:
                    return
                group_warnings = await sync_items(
                    items,
                    media_type,
                    CollectionSource.nuvio,
                    db,
                    stats,
                    user_id,
                    job_id,
                    show_map if media_type == MediaType.episode else {},
                    api_key=tmdb_api_key,
                    show_id_to_tmdb=show_id_to_tmdb if media_type == MediaType.episode else {},
                    sync_collection=sync_collection,
                    sync_watched=sync_watched,
                    sync_ratings=False,
                    new_watched_ids=new_watched_ids,
                    connection_id=conn.id,
                )
                warnings.extend(group_warnings)

            for media_type in (MediaType.movie, MediaType.series, MediaType.episode):
                await sync_group(
                    normalized_library,
                    media_type,
                    sync_collection=True,
                    sync_watched=False,
                )
            for media_type in (MediaType.movie, MediaType.series, MediaType.episode):
                await sync_group(
                    normalized_watched,
                    media_type,
                    sync_collection=False,
                    sync_watched=True,
                )
            for media_type in (MediaType.movie, MediaType.series, MediaType.episode):
                await sync_group(
                    normalized_progress,
                    media_type,
                    sync_collection=False,
                    sync_watched=False,
                )

            if progress_records:
                await _apply_nuvio_progress(db, user_id, progress_records, show_map, tmdb_ids)

            await _fan_out_changes_to_other_connections(
                db,
                user_id,
                conn.id,
                new_watched_ids,
                {},
                settings=settings,
                exclude_cloud_source=CollectionSource.nuvio,
            )
            warnings = await _stamp_matched_show_warnings(db, user_id, warnings)
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(
                    status=SyncStatus.completed,
                    stats=stats,
                    warnings=warnings or None,
                    updated_at=func.now(),
                )
            )
            await db.commit()
            asyncio.create_task(pre_cache_all_collected_bg())
            logger.info("Nuvio sync job %s completed. Stats: %s", job_id, stats)
        except Exception as exc:
            logger.exception("Nuvio sync job %s failed", job_id)
            await db.rollback()
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(status=SyncStatus.failed, error_message=str(exc)[:900])
            )
            await db.commit()


class LibrarySelectionBody(BaseModel):
    library_ids: list[str]


class PlexLibrarySelectionBody(BaseModel):
    library_keys: list[str]


async def _get_connection_or_404(db: AsyncSession, connection_id: int, user_id: int) -> MediaServerConnection:
    result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.id == connection_id,
            MediaServerConnection.user_id == user_id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


@router.get("/connection/{connection_id}/plex-friends")
async def get_plex_friends(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_connection_or_404(db, connection_id, current_user.id)
    if conn.type != "plex":
        raise HTTPException(status_code=400, detail="Connection is not a Plex server")
    from core import plex as plex_client
    friends = await plex_client.get_all_friends(conn.token)
    return {"friends": friends}


@router.get("/connection/{connection_id}/libraries")
async def get_connection_libraries(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_connection_or_404(db, connection_id, current_user.id)

    try:
        if conn.type == "jellyfin":
            available = await jellyfin.get_libraries(conn.url, conn.token, conn.server_user_id)
            sel_result = await db.execute(
                select(JellyfinLibrarySelection).where(JellyfinLibrarySelection.connection_id == conn.id)
            )
            selected_ids = {row.library_id for row in sel_result.scalars().all()}
            libraries = [
                {"id": lib["Id"], "name": lib["Name"], "type": lib.get("CollectionType"), "selected": lib["Id"] in selected_ids}
                for lib in available if lib.get("CollectionType") in ("movies", "tvshows", "tv")
            ]
            return {"libraries": libraries, "all_selected": len(selected_ids) == 0}

        elif conn.type == "emby":
            available = await emby.get_libraries(conn.url, conn.token, conn.server_user_id)
            sel_result = await db.execute(
                select(EmbyLibrarySelection).where(EmbyLibrarySelection.connection_id == conn.id)
            )
            selected_ids = {row.library_id for row in sel_result.scalars().all()}
            libraries = [
                {"id": lib["Id"], "name": lib["Name"], "type": lib.get("CollectionType"), "selected": lib["Id"] in selected_ids}
                for lib in available if lib.get("CollectionType") in ("movies", "tvshows", "tv")
            ]
            return {"libraries": libraries, "all_selected": len(selected_ids) == 0}

        elif conn.type == "plex":
            available = await plex.get_libraries(conn.url, conn.token)
            sel_result = await db.execute(
                select(PlexLibrarySelection).where(PlexLibrarySelection.connection_id == conn.id)
            )
            selected_keys = {row.library_key for row in sel_result.scalars().all()}
            libraries = [
                {"key": lib["key"], "name": lib["title"], "type": lib.get("type"), "selected": lib["key"] in selected_keys}
                for lib in available if lib.get("type") in ("movie", "show")
            ]
            return {"libraries": libraries, "all_selected": len(selected_keys) == 0}

        elif conn.type == "nuvio":
            return {"libraries": [], "all_selected": True}

        else:
            raise HTTPException(status_code=400, detail=f"Unknown connection type: {conn.type}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach server: {e}")


@router.post("/connection/{connection_id}/scan")
async def trigger_library_scan(
    connection_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_connection_or_404(db, connection_id, current_user.id)

    try:
        if conn.type == "nuvio":
            settings_result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == current_user.id)
            )
            settings = settings_result.scalar_one_or_none()
            if not await _get_effective_tmdb_key(db, settings):
                raise HTTPException(status_code=400, detail="TMDB API key required")
            active_result = await db.execute(
                select(SyncJob)
                .where(
                    SyncJob.user_id == current_user.id,
                    SyncJob.connection_id == conn.id,
                    SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]),
                )
                .limit(1)
            )
            active_job = active_result.scalar_one_or_none()
            if active_job:
                return {
                    "status": "started",
                    "job_id": active_job.id,
                    "message": "Nuvio sync is already running",
                }
            job = SyncJob(
                user_id=current_user.id,
                source=CollectionSource.nuvio,
                status=SyncStatus.pending,
                connection_id=conn.id,
                job_type="pull",
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            background_tasks.add_task(
                run_nuvio_sync,
                current_user.id,
                job.id,
                0,
                0,
                conn.id,
            )
            return {
                "status": "started",
                "job_id": job.id,
                "message": "Nuvio library, watch history, and playback progress sync started",
            }
        if conn.type in ("jellyfin", "emby"):
            client = jellyfin if conn.type == "jellyfin" else emby
            ok = await client.scan_libraries(conn.url, conn.token)
        elif conn.type == "plex":
            sel_result = await db.execute(
                select(PlexLibrarySelection).where(PlexLibrarySelection.connection_id == conn.id)
            )
            selected_keys = [row.library_key for row in sel_result.scalars().all()]
            ok = await plex.scan_libraries(conn.url, conn.token, selected_keys)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown connection type: {conn.type}")

        if not ok:
            raise HTTPException(status_code=502, detail="Library scan request failed")
        return {"status": "ok", "message": "Library scan triggered successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach server: {e}")


@router.put("/connection/{connection_id}/libraries")
async def save_connection_libraries(
    connection_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_connection_or_404(db, connection_id, current_user.id)

    try:
        if conn.type == "jellyfin":
            library_ids: list[str] = body.get("library_ids", [])
            available = await jellyfin.get_libraries(conn.url, conn.token, conn.server_user_id)
            name_map = {lib["Id"]: lib["Name"] for lib in available}
            await db.execute(delete(JellyfinLibrarySelection).where(JellyfinLibrarySelection.connection_id == conn.id))
            for lid in library_ids:
                if lid in name_map:
                    db.add(JellyfinLibrarySelection(user_id=current_user.id, connection_id=conn.id, library_id=lid, library_name=name_map[lid]))
            await db.commit()
            return {"saved": len(library_ids)}

        elif conn.type == "emby":
            library_ids = body.get("library_ids", [])
            available = await emby.get_libraries(conn.url, conn.token, conn.server_user_id)
            name_map = {lib["Id"]: lib["Name"] for lib in available}
            await db.execute(delete(EmbyLibrarySelection).where(EmbyLibrarySelection.connection_id == conn.id))
            for lid in library_ids:
                if lid in name_map:
                    db.add(EmbyLibrarySelection(user_id=current_user.id, connection_id=conn.id, library_id=lid, library_name=name_map[lid]))
            await db.commit()
            return {"saved": len(library_ids)}

        elif conn.type == "plex":
            library_keys: list[str] = body.get("library_keys", [])
            available = await plex.get_libraries(conn.url, conn.token)
            name_map = {lib["key"]: lib["title"] for lib in available}
            await db.execute(delete(PlexLibrarySelection).where(PlexLibrarySelection.connection_id == conn.id))
            for key in library_keys:
                if key in name_map:
                    db.add(PlexLibrarySelection(user_id=current_user.id, connection_id=conn.id, library_key=key, library_name=name_map[key]))
            await db.commit()
            return {"saved": len(library_keys)}

        elif conn.type == "nuvio":
            return {"saved": 0}

        else:
            raise HTTPException(status_code=400, detail=f"Unknown connection type: {conn.type}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach server: {e}")


@router.post("/connection/{connection_id}")
async def sync_connection(
    connection_id: int,
    background_tasks: BackgroundTasks,
    movie_limit: int = Query(default=0),
    show_limit: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_connection_or_404(db, connection_id, current_user.id)

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if not await _get_effective_tmdb_key(db, settings):
        raise HTTPException(status_code=400, detail="TMDB API key required")

    source_map = {"jellyfin": CollectionSource.jellyfin, "emby": CollectionSource.emby, "plex": CollectionSource.plex, "nuvio": CollectionSource.nuvio}
    source = source_map.get(conn.type)
    if not source:
        raise HTTPException(status_code=400, detail=f"Unknown connection type: {conn.type}")

    job = SyncJob(user_id=current_user.id, source=source, status=SyncStatus.pending, connection_id=connection_id, job_type="pull")
    db.add(job)
    await db.commit()
    await db.refresh(job)

    runner_map = {"jellyfin": run_jellyfin_sync, "emby": run_emby_sync, "plex": run_plex_sync, "nuvio": run_nuvio_sync}
    background_tasks.add_task(runner_map[conn.type], current_user.id, job.id, movie_limit, show_limit, connection_id)
    return {"status": "started", "job_id": job.id, "message": f"{conn.type.capitalize()} sync is running in the background"}


async def _run_full_push(user_id: int, connection_id: int, job_id: int) -> None:
    import httpx as _httpx

    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running))
        await db.commit()

        try:
            conn_result = await db.execute(
                select(MediaServerConnection).where(
                    MediaServerConnection.id == connection_id,
                    MediaServerConnection.user_id == user_id,
                )
            )
            conn = conn_result.scalar_one_or_none()
            if not conn:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message="Connection not found"))
                await db.commit()
                return

            if conn.type == "nuvio":
                settings_result = await db.execute(
                    select(UserSettings).where(UserSettings.user_id == user_id)
                )
                user_settings = settings_result.scalar_one_or_none()
                api_key = await _get_effective_tmdb_key(db, user_settings)
                watched_items = (
                    await _build_nuvio_watched_items(db, user_id, api_key=api_key)
                    if conn.push_watched
                    else []
                )
                progress_items = (
                    await _build_nuvio_progress_items(db, user_id, api_key=api_key)
                    if conn.push_playback
                    else []
                )
                total = len(watched_items) + len(progress_items)
                await db.execute(
                    update(SyncJob)
                    .where(SyncJob.id == job_id)
                    .values(total_items=total, processed_items=0)
                )
                await db.commit()
                if watched_items or progress_items:
                    session = await nuvio.push_sync_items(
                        conn.url,
                        conn.token,
                        _nuvio_profile_id(conn),
                        watched_items,
                        progress_items,
                    )
                    conn.token = session.refresh_token
                await db.execute(
                    update(SyncJob)
                    .where(SyncJob.id == job_id)
                    .values(
                        status=SyncStatus.completed,
                        processed_items=total,
                        stats={
                            "succeeded": total,
                            "failed": 0,
                            "watched": len(watched_items),
                            "progress": len(progress_items),
                        },
                    )
                )
                await db.commit()
                logger.info(
                    "Full Nuvio push for connection %s: %s watched and %s progress items",
                    connection_id,
                    len(watched_items),
                    len(progress_items),
                )
                return

            conn_source = CollectionSource(conn.type)
            watched_ids: set[int] = set()
            ratings_map: RatingChanges = {}

            if conn.push_watched:
                watched_result = await db.execute(
                    select(WatchEvent.media_id).where(WatchEvent.user_id == user_id).distinct()
                )
                watched_ids = {row[0] for row in watched_result.all()}

            if conn.push_ratings:
                ratings_result = await db.execute(
                    select(Rating.media_id, Rating.season_number, Rating.rating).where(
                        Rating.user_id == user_id,
                        Rating.rating.isnot(None),
                    )
                )
                ratings_map = {
                    (media_id, season_number): float(rating)
                    for media_id, season_number, rating in ratings_result.all()
                }

            all_media_ids = watched_ids | {media_id for media_id, _ in ratings_map}
            if not all_media_ids:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, total_items=0, processed_items=0))
                await db.commit()
                print(f"Full push for connection {connection_id}: nothing to push")
                return

            # Fast path: items we've already synced from this server have a known source_id
            source_ids_map: dict[int, list[str]] = {}
            all_media_list = list(all_media_ids)
            for i in range(0, len(all_media_list), _MAX_IN_PARAMS):
                chunk = all_media_list[i : i + _MAX_IN_PARAMS]
                files_chunk = await db.execute(
                    select(CollectionFile.source_id, Collection.media_id)
                    .join(Collection, Collection.id == CollectionFile.collection_id)
                    .where(
                        Collection.user_id == user_id,
                        Collection.media_id.in_(chunk),
                        CollectionFile.source == conn_source,
                        CollectionFile.source_id.isnot(None),
                    )
                )
                for source_id, media_id in files_chunk.all():
                    source_ids_map.setdefault(media_id, []).append(source_id)

            # Slow path: unknown items and Plex season ratings need media metadata.
            missing_ids = all_media_ids - set(source_ids_map)
            season_rating_ids = {
                media_id
                for media_id, season_number in ratings_map
                if season_number is not None
            }
            lookup_media_ids = missing_ids | season_rating_ids
            media_info: dict[int, Media] = {}
            show_tmdb_map: dict[int, int] = {}  # show.id → show.tmdb_id

            if lookup_media_ids:
                media_rows_list = await _select_in_chunks(
                    db,
                    lambda chunk: select(Media).where(Media.id.in_(chunk)),
                    list(lookup_media_ids),
                )
                for media in media_rows_list:
                    media_info[media.id] = media

                show_ids_needed = {m.show_id for m in media_info.values() if m.show_id is not None}
                if show_ids_needed:
                    show_ids_list = list(show_ids_needed)
                    for i in range(0, len(show_ids_list), _MAX_IN_PARAMS):
                        chunk = show_ids_list[i : i + _MAX_IN_PARAMS]
                        show_rows = await db.execute(select(Show.id, Show.tmdb_id).where(Show.id.in_(chunk)))
                        for row in show_rows.all():
                            show_tmdb_map[row[0]] = row[1]

            # Build push list: (action, source_id, [rating])
            push_items: list[tuple] = []

            if conn.push_watched:
                for mid in watched_ids:
                    for sid in source_ids_map.get(mid, []):
                        push_items.append(("watched", sid))

            if conn.push_ratings:
                for (mid, season_number), rating in ratings_map.items():
                    if season_number is not None:
                        continue
                    for sid in source_ids_map.get(mid, []):
                        push_items.append(("rating", sid, rating))

            # Items that need live lookup: defer as coroutines resolved during push.
            lookup_items: list[tuple] = []

            if missing_ids:
                if conn.push_watched:
                    for mid in watched_ids & missing_ids:
                        if mid in media_info:
                            lookup_items.append(("watched", mid))
                if conn.push_ratings:
                    for key, rating in ratings_map.items():
                        mid, season_number = key
                        if season_number is None and mid in missing_ids and mid in media_info:
                            lookup_items.append(("rating", mid, rating))
            if conn.type == "plex" and conn.push_ratings:
                for (mid, season_number), rating in ratings_map.items():
                    if season_number is not None and mid in media_info:
                        lookup_items.append(("season_rating", mid, season_number, rating))

            total = len(push_items) + len(lookup_items)
            if total == 0:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, total_items=0, processed_items=0))
                await db.commit()
                print(f"Full push for connection {connection_id}: no items found for this server")
                return

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total, processed_items=0))
            await db.commit()
            print(f"Full push for connection {connection_id}: pushing {total} items ({len(push_items)} known, {len(lookup_items)} via live lookup)...")

            sem = asyncio.Semaphore(10)
            _PROGRESS_INTERVAL = 20

            def _extract_source_id(item_dict: dict | None) -> str | None:
                if not item_dict:
                    return None
                if conn.type == "plex":
                    rk = item_dict.get("ratingKey")
                    return str(rk) if rk else None
                return item_dict.get("Id")

            async def _find_source_id(mid: int) -> str | None:
                m = media_info.get(mid)
                if not m or not m.tmdb_id:
                    return None
                if m.media_type == MediaType.movie:
                    if conn.type == "plex":
                        found = await plex.find_movie_by_tmdb_id(conn.url, conn.token, m.tmdb_id)
                    elif conn.type == "jellyfin":
                        found = await jellyfin.find_movie_by_tmdb_id(conn.url, conn.token, m.tmdb_id)
                    else:
                        found = await emby.find_movie_by_tmdb_id(conn.url, conn.token, m.tmdb_id)
                elif m.media_type == MediaType.episode:
                    show_tmdb = show_tmdb_map.get(m.show_id) if m.show_id else None
                    if not show_tmdb or m.season_number is None or m.episode_number is None:
                        return None
                    if conn.type == "plex":
                        found = await plex.find_episode_by_ids(conn.url, conn.token, show_tmdb, m.season_number, m.episode_number)
                    elif conn.type == "jellyfin":
                        found = await jellyfin.find_episode_by_ids(conn.url, conn.token, show_tmdb, m.season_number, m.episode_number)
                    else:
                        found = await emby.find_episode_by_ids(conn.url, conn.token, show_tmdb, m.season_number, m.episode_number)
                else:
                    return None
                return _extract_source_id(found)

            async def _push_known(client: _httpx.AsyncClient, item: tuple) -> bool:
                async with sem:
                    try:
                        if item[0] == "watched":
                            sid = item[1]
                            if conn.type == "plex":
                                return await plex.mark_watched(conn.url, conn.token, sid, client=client)
                            elif conn.type == "jellyfin":
                                return await jellyfin.mark_watched(conn.url, conn.token, conn.server_user_id, sid, client=client)
                            else:
                                return await emby.mark_watched(conn.url, conn.token, conn.server_user_id, sid, client=client)
                        else:
                            sid, rating = item[1], item[2]
                            if conn.type == "plex":
                                return await plex.set_rating(conn.url, conn.token, sid, rating, client=client)
                            elif conn.type == "jellyfin":
                                return await jellyfin.set_rating(conn.url, conn.token, conn.server_user_id, sid, rating, client=client)
                            else:
                                return await emby.set_rating(conn.url, conn.token, conn.server_user_id, sid, rating, client=client)
                    except Exception:
                        return False

            async def _push_lookup(client: _httpx.AsyncClient, item: tuple) -> bool:
                async with sem:
                    try:
                        mid = item[1]
                        if item[0] == "season_rating":
                            media = media_info.get(mid)
                            if not media or not media.tmdb_id:
                                return False
                            sid = await plex.resolve_season_rating_key(
                                conn.url,
                                conn.token,
                                media.tmdb_id,
                                item[2],
                            )
                            if not sid:
                                return False
                            return await plex.set_rating(
                                conn.url,
                                conn.token,
                                sid,
                                item[3],
                                client=client,
                            )
                        sid = await _find_source_id(mid)
                        if not sid:
                            return False
                        if item[0] == "watched":
                            if conn.type == "plex":
                                return await plex.mark_watched(conn.url, conn.token, sid, client=client)
                            elif conn.type == "jellyfin":
                                return await jellyfin.mark_watched(conn.url, conn.token, conn.server_user_id, sid, client=client)
                            else:
                                return await emby.mark_watched(conn.url, conn.token, conn.server_user_id, sid, client=client)
                        else:
                            rating = item[2]
                            if conn.type == "plex":
                                return await plex.set_rating(conn.url, conn.token, sid, rating, client=client)
                            elif conn.type == "jellyfin":
                                return await jellyfin.set_rating(conn.url, conn.token, conn.server_user_id, sid, rating, client=client)
                            else:
                                return await emby.set_rating(conn.url, conn.token, conn.server_user_id, sid, rating, client=client)
                    except Exception:
                        return False

            done = 0
            succeeded = 0
            failed_count = 0

            async with _httpx.AsyncClient(timeout=_httpx.Timeout(15.0), follow_redirects=False) as client:
                coros = (
                    [_push_known(client, item) for item in push_items]
                    + [_push_lookup(client, item) for item in lookup_items]
                )
                for future in asyncio.as_completed(coros):
                    result = await future
                    done += 1
                    if result is True:
                        succeeded += 1
                    else:
                        failed_count += 1
                    if done % _PROGRESS_INTERVAL == 0:
                        await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=done))
                        await db.commit()

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                status=SyncStatus.completed,
                processed_items=total,
                stats={"succeeded": succeeded, "failed": failed_count},
            ))
            await db.commit()
            print(f"Full push for connection {connection_id}: {succeeded}/{total} succeeded, {failed_count} failed")

        except Exception as e:
            import traceback
            traceback.print_exc()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=str(e)[:900]))
            await db.commit()


@router.post("/connection/{connection_id}/push")
async def push_upstream(
    connection_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_connection_or_404(db, connection_id, current_user.id)
    if not conn.push_watched and not conn.push_ratings and not conn.push_playback:
        raise HTTPException(status_code=400, detail="Enable 'Scrob → Server' push flags for this connection first")

    source_map = {"jellyfin": CollectionSource.jellyfin, "emby": CollectionSource.emby, "plex": CollectionSource.plex, "nuvio": CollectionSource.nuvio}
    source = source_map.get(conn.type, CollectionSource.jellyfin)
    job = SyncJob(user_id=current_user.id, source=source, status=SyncStatus.pending, connection_id=connection_id, job_type="push")
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(_run_full_push, current_user.id, connection_id, job.id)
    return {"status": "started", "job_id": job.id, "message": "Full upstream push is running in the background"}


@router.post("/jellyfin")
async def sync_jellyfin(
    background_tasks: BackgroundTasks,
    movie_limit: int = Query(default=0),
    show_limit: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if not await _get_effective_tmdb_key(db, settings):
        raise HTTPException(status_code=400, detail="TMDB API key required")

    conn_result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == current_user.id,
            MediaServerConnection.type == "jellyfin",
        ).order_by(MediaServerConnection.id.asc()).limit(1)
    )
    if not conn_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="No Jellyfin connection configured")

    job = SyncJob(user_id=current_user.id, source=CollectionSource.jellyfin, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_jellyfin_sync, current_user.id, job.id, movie_limit, show_limit)
    return {"status": "started", "job_id": job.id, "message": "Jellyfin sync is running in the background"}


@router.post("/emby")
async def sync_emby(
    background_tasks: BackgroundTasks,
    movie_limit: int = Query(default=0),
    show_limit: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if not await _get_effective_tmdb_key(db, settings):
        raise HTTPException(status_code=400, detail="TMDB API key required")

    conn_result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == current_user.id,
            MediaServerConnection.type == "emby",
        ).order_by(MediaServerConnection.id.asc()).limit(1)
    )
    if not conn_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="No Emby connection configured")

    job = SyncJob(user_id=current_user.id, source=CollectionSource.emby, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_emby_sync, current_user.id, job.id, movie_limit, show_limit)
    return {"status": "started", "job_id": job.id, "message": "Emby sync is running in the background"}


@router.post("/plex")
async def sync_plex(
    background_tasks: BackgroundTasks,
    movie_limit: int = Query(default=0),
    show_limit: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if not await _get_effective_tmdb_key(db, settings):
        raise HTTPException(status_code=400, detail="TMDB API key required")

    conn_result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == current_user.id,
            MediaServerConnection.type == "plex",
        ).order_by(MediaServerConnection.id.asc()).limit(1)
    )
    if not conn_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="No Plex connection configured")

    job = SyncJob(user_id=current_user.id, source=CollectionSource.plex, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_plex_sync, current_user.id, job.id, movie_limit, show_limit)
    return {"status": "started", "job_id": job.id, "message": "Plex sync is running in the background"}


@router.get("/status")
async def get_sync_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(SyncJob).where(SyncJob.user_id == current_user.id).order_by(SyncJob.created_at.desc()).limit(5)
    result = await db.execute(query)
    jobs = result.scalars().all()
    return jobs


@router.post("/heal")
async def heal_metadata(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-enrich all collection items that are missing poster/date metadata."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    if not await _get_effective_tmdb_key(db, settings):
        raise HTTPException(status_code=400, detail="TMDB API key required")

    effective_key = await _get_effective_tmdb_key(db, settings)
    job = SyncJob(user_id=current_user.id, source=CollectionSource.tmdb, job_type="heal", status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(run_heal, current_user.id, effective_key, job.id)
    return {"status": "started", "message": "Metadata heal is running in the background"}


async def run_heal(user_id: int, api_key: str, job_id: int | None = None):
    from models.show import Show
    from routers.webhooks import _find_or_create_show
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        async def _update_job(**kwargs):
            if job_id is None:
                return
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(updated_at=func.now(), **kwargs))
            await db.commit()

        try:
            await _update_job(status=SyncStatus.running)

            # ── Phase 1: Re-enrich items that have show linkage but missing poster ──
            coll_q = await db.execute(
                select(Media)
                .join(Collection, Collection.media_id == Media.id)
                .where(
                    Collection.user_id == user_id,
                    Media.poster_path.is_(None),
                )
            )
            items = coll_q.scalars().all()

            movies = [m for m in items if m.media_type == MediaType.movie and m.tmdb_id]
            episodes = [m for m in items if m.media_type == MediaType.episode and m.show_id and m.season_number is not None and m.episode_number is not None]

            if movies or episodes:
                print(f"Heal: {len(movies)} movies, {len(episodes)} episodes to re-enrich for user {user_id}")

                show_ids = list({m.show_id for m in episodes})
                show_tmdb_map: dict[int, int] = {}
                if show_ids:
                    shows_q = await db.execute(select(Show).where(Show.id.in_(show_ids)))
                    for s in shows_q.scalars().all():
                        if s.tmdb_id:
                            show_tmdb_map[s.id] = s.tmdb_id

                to_enrich = [(m, None) for m in movies] + [
                    (m, show_tmdb_map[m.show_id]) for m in episodes if m.show_id in show_tmdb_map
                ]
                await _update_job(total_items=len(to_enrich), processed_items=0)
                await batch_enrich_items(to_enrich, api_key=api_key)
                await db.commit()
                await _update_job(processed_items=len(to_enrich))
                print(f"Heal: re-enriched {len(to_enrich)} items for user {user_id}")
            else:
                print(f"Heal: nothing to re-enrich for user {user_id}")
                await _update_job(total_items=0, processed_items=0)

            # ── Phase 2: Recover orphaned episodes via Jellyfin/Emby ─────────────
            # Webhook-created episodes may have show_id=None if the show wasn't in
            # the DB yet. Look them up by their source ID to re-link and enrich them.
            orphan_q = await db.execute(
                select(Media, CollectionFile, MediaServerConnection)
                .join(Collection, Collection.media_id == Media.id)
                .join(CollectionFile, CollectionFile.collection_id == Collection.id)
                .join(MediaServerConnection, MediaServerConnection.id == CollectionFile.connection_id)
                .where(
                    Collection.user_id == user_id,
                    Media.media_type == MediaType.episode,
                    Media.show_id.is_(None),
                    Media.season_number.isnot(None),
                    Media.episode_number.isnot(None),
                    CollectionFile.source.in_([CollectionSource.jellyfin, CollectionSource.emby]),
                    CollectionFile.connection_id.isnot(None),
                )
            )
            orphan_rows = orphan_q.all()

            if orphan_rows:
                recovered = 0
                seen: set[int] = set()
                for orphan_media, coll_file, conn in orphan_rows:
                    if orphan_media.id in seen:
                        continue
                    seen.add(orphan_media.id)
                    try:
                        item_data = await jellyfin.get_item(conn.url, conn.token, coll_file.source_id)
                        if not item_data:
                            continue
                        series_id = item_data.get("SeriesId")
                        if not series_id:
                            continue
                        series_data = await jellyfin.get_item(conn.url, conn.token, series_id)
                        if not series_data:
                            continue
                        series_tmdb_raw = series_data.get("ProviderIds", {}).get("Tmdb")
                        if not series_tmdb_raw:
                            continue
                        series_tmdb_id = int(series_tmdb_raw)
                        show = await _find_or_create_show(db, series_tmdb_id, api_key)
                        orphan_media.show_id = show.id
                        await enrich_media(orphan_media, api_key=api_key, series_tmdb_id=series_tmdb_id)
                        recovered += 1
                    except Exception as e:
                        print(f"Heal: failed to recover orphan '{orphan_media.title}' (id={orphan_media.id}): {e}")
                if recovered:
                    await db.commit()
                print(f"Heal: recovered {recovered}/{len(seen)} orphaned episode(s) for user {user_id}")

            await _update_job(status=SyncStatus.completed, stats={"healed": True})
            asyncio.create_task(pre_cache_all_collected_bg())

        except Exception as e:
            print(f"Heal failed for user {user_id}: {e}")
            import traceback
            traceback.print_exc()
            await _update_job(status=SyncStatus.failed, error_message=str(e)[:900])


@router.post("/abort")
async def abort_sync(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Aborts any pending or running sync jobs for the current user."""
    await db.execute(
        update(SyncJob)
        .where(SyncJob.user_id == current_user.id)
        .where(SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]))
        .values(status=SyncStatus.failed, error_message="Aborted by user", updated_at=func.now())
    )
    await db.commit()
    return {"status": "ok", "message": "All active sync jobs have been marked as aborted"}


async def _stamp_matched_show_warnings(db: AsyncSession, user_id: int, warnings: list[dict]) -> list[dict]:
    """Auto-stamp warnings for shows that have already been TVDB-matched by this user.

    On every sync, series/episode warnings are regenerated fresh without matched state.
    This helper checks each warning title against already-matched Media rows and stamps
    matched:true + tvdb/show info so the panel renders the correct badge without requiring
    the user to re-run the match action.
    """
    from sqlalchemy import func as sa_func

    titles = set()
    for w in warnings:
        t = w.get("title") or w.get("series_name")
        if t:
            titles.add(t.lower())

    if not titles:
        return warnings

    # Find any episode Media row per matched title (show_id set, show has tvdb_id)
    matched_ep_result = await db.execute(
        select(Media, Show)
        .join(Collection, Collection.media_id == Media.id)
        .join(Show, Show.id == Media.show_id)
        .where(
            Collection.user_id == user_id,
            Media.media_type == MediaType.episode,
            Media.show_id.isnot(None),
            Show.tvdb_id.isnot(None),
            sa_func.lower(Media.tmdb_data["show_title"].astext).in_(list(titles)),
        )
        .limit(len(titles) * 5)
    )
    title_to_show: dict[str, Show] = {}
    for media, show in matched_ep_result.all():
        key = (media.tmdb_data or {}).get("show_title", "").lower()
        if key and key not in title_to_show:
            title_to_show[key] = show

    if not title_to_show:
        return warnings

    stamped = []
    for w in warnings:
        raw_title = w.get("title") or w.get("series_name") or ""
        show = title_to_show.get(raw_title.lower())
        if show and not w.get("matched"):
            stamped.append({
                **w,
                "matched": True,
                "matched_tvdb_id": show.tvdb_id,
                "matched_show_id": show.tmdb_id,
                "matched_show_title": show.title,
            })
        else:
            stamped.append(w)
    return stamped


# ── Season override endpoints ─────────────────────────────────────────────────

class SeasonOverrideBody(BaseModel):
    source_show_tmdb_id: int
    source_season_number: int
    target_show_tmdb_id: int
    target_season_number: int


@router.get("/season-overrides")
async def list_season_overrides(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ShowSeasonOverride).where(ShowSeasonOverride.user_id == current_user.id)
    )
    overrides = result.scalars().all()

    # Resolve show titles for all distinct TMDB IDs referenced by overrides
    all_tmdb_ids = {o.source_show_tmdb_id for o in overrides} | {o.target_show_tmdb_id for o in overrides}
    show_title_map: dict[int, str] = {}
    if all_tmdb_ids:
        shows_res = await db.execute(select(Show.tmdb_id, Show.title).where(Show.tmdb_id.in_(list(all_tmdb_ids))))
        for tmdb_id, title in shows_res.all():
            if tmdb_id is not None:
                show_title_map[tmdb_id] = title

    return [
        {
            "id": o.id,
            "source_show_tmdb_id": o.source_show_tmdb_id,
            "source_season_number": o.source_season_number,
            "source_show_title": show_title_map.get(o.source_show_tmdb_id),
            "target_show_tmdb_id": o.target_show_tmdb_id,
            "target_season_number": o.target_season_number,
            "target_show_title": show_title_map.get(o.target_show_tmdb_id),
        }
        for o in overrides
    ]


@router.post("/season-overrides")
async def create_season_override(
    body: SeasonOverrideBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = await db.execute(
        select(ShowSeasonOverride).where(
            ShowSeasonOverride.user_id == current_user.id,
            ShowSeasonOverride.source_show_tmdb_id == body.source_show_tmdb_id,
            ShowSeasonOverride.source_season_number == body.source_season_number,
        )
    )
    override = existing.scalar_one_or_none()
    if override:
        override.target_show_tmdb_id = body.target_show_tmdb_id
        override.target_season_number = body.target_season_number
    else:
        override = ShowSeasonOverride(
            user_id=current_user.id,
            source_show_tmdb_id=body.source_show_tmdb_id,
            source_season_number=body.source_season_number,
            target_show_tmdb_id=body.target_show_tmdb_id,
            target_season_number=body.target_season_number,
        )
        db.add(override)
    await db.commit()
    await db.refresh(override)
    return {
        "id": override.id,
        "source_show_tmdb_id": override.source_show_tmdb_id,
        "source_season_number": override.source_season_number,
        "target_show_tmdb_id": override.target_show_tmdb_id,
        "target_season_number": override.target_season_number,
    }


@router.delete("/season-overrides/{override_id}")
async def delete_season_override(
    override_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ShowSeasonOverride).where(
            ShowSeasonOverride.id == override_id,
            ShowSeasonOverride.user_id == current_user.id,
        )
    )
    override = result.scalar_one_or_none()
    if not override:
        raise HTTPException(status_code=404, detail="Override not found")
    await db.delete(override)
    await db.commit()
    return {"status": "ok"}


@router.post("/season-overrides/{override_id}/apply")
async def apply_season_override(
    override_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remap existing collection episodes to the target show/season and re-enrich metadata."""
    result = await db.execute(
        select(ShowSeasonOverride).where(
            ShowSeasonOverride.id == override_id,
            ShowSeasonOverride.user_id == current_user.id,
        )
    )
    override = result.scalar_one_or_none()
    if not override:
        raise HTTPException(status_code=404, detail="Override not found")

    tmdb_api_key = await _get_effective_tmdb_key(db, None)
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if settings and settings.tmdb_api_key:
        tmdb_api_key = settings.tmdb_api_key
    if not tmdb_api_key:
        raise HTTPException(status_code=400, detail="TMDB API key required")

    # Find source show by tmdb_id
    source_show_result = await db.execute(
        select(Show).where(Show.tmdb_id == override.source_show_tmdb_id)
    )
    source_show = source_show_result.scalar_one_or_none()
    if not source_show:
        raise HTTPException(status_code=404, detail="Source show not found in local DB")

    # Find all user-collection episodes for (source_show, source_season)
    ep_result = await db.execute(
        select(Media)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.show_id == source_show.id,
            Media.season_number == override.source_season_number,
            Media.media_type == MediaType.episode,
        )
    )
    episodes = ep_result.scalars().all()
    if not episodes:
        return {"status": "ok", "remapped": 0}

    # Find or create the target Show
    target_show_result = await db.execute(
        select(Show).where(Show.tmdb_id == override.target_show_tmdb_id)
    )
    target_show = target_show_result.scalar_one_or_none()
    if not target_show:
        try:
            show_data = await tmdb.get_show(override.target_show_tmdb_id, api_key=tmdb_api_key)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not fetch target show from TMDB: {e}")
        seasons_meta = [
            {
                "season_number": s["season_number"],
                "name": s.get("name"),
                "overview": s.get("overview"),
                "poster_path": tmdb.poster_url(s.get("poster_path")),
                "episode_count": s.get("episode_count"),
                "air_date": s.get("air_date"),
            }
            for s in show_data.get("seasons", [])
        ]
        target_show = Show(
            tmdb_id=override.target_show_tmdb_id,
            title=show_data.get("name") or show_data.get("original_name"),
            original_title=show_data.get("original_name"),
            overview=show_data.get("overview"),
            poster_path=tmdb.poster_url(show_data.get("poster_path")),
            backdrop_path=tmdb.poster_url(show_data.get("backdrop_path"), size="w1280"),
            tmdb_rating=show_data.get("vote_average"),
            status=show_data.get("status"),
            tagline=show_data.get("tagline"),
            first_air_date=show_data.get("first_air_date"),
            last_air_date=show_data.get("last_air_date"),
            tmdb_data={**show_data, "seasons": seasons_meta, "genres": [g["name"] if isinstance(g, dict) else g for g in show_data.get("genres", [])]},
        )
        db.add(target_show)
        await db.flush()

    # Fetch TMDB season data for the target season
    try:
        season_data = await tmdb.get_season(override.target_show_tmdb_id, override.target_season_number, api_key=tmdb_api_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch target season from TMDB: {e}")

    ep_map = {ep["episode_number"]: ep for ep in season_data.get("episodes", [])}

    # Remap and re-enrich episodes
    for media in episodes:
        media.show_id = target_show.id
        media.season_number = override.target_season_number
        ep = ep_map.get(media.episode_number)
        if ep:
            media.tmdb_id = ep.get("id") or media.tmdb_id
            media.title = ep.get("name") or media.title
            media.overview = ep.get("overview")
            media.poster_path = tmdb.poster_url(ep.get("still_path"), size="w500")
            media.release_date = ep.get("air_date")
            media.tmdb_rating = ep.get("vote_average")
            media.tmdb_data = {"runtime": ep.get("runtime"), "cast": []}

    await db.commit()
    return {"status": "ok", "remapped": len(episodes)}


# ── Unmatched show matching ───────────────────────────────────────────────────

class MatchUnmatchedBody(BaseModel):
    show_title: str
    tmdb_id: int | None = None
    tvdb_id: int | None = None


@router.post("/match-unmatched-show")
async def match_unmatched_show(
    body: MatchUnmatchedBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Link unmatched local episodes (no tmdb_id/show_id) to a TMDB or TVDB show."""
    if not body.tmdb_id and not body.tvdb_id:
        raise HTTPException(status_code=400, detail="Either tmdb_id or tvdb_id is required")

    from sqlalchemy import cast as sa_cast, Text as SAText, func as sa_func

    ep_result = await db.execute(
        select(Media)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.tmdb_id.is_(None),
            Media.show_id.is_(None),
            Media.media_type == MediaType.episode,
            sa_func.lower(Media.tmdb_data["show_title"].astext) == body.show_title.lower(),
        )
        .distinct()
    )
    episodes = ep_result.scalars().all()
    if not episodes:
        # Episodes may already be linked (matched in a previous session before warning stamping
        # existed). Detect that case: find any matched episode for this show_title and stamp
        # warnings so the panel reflects the existing match.
        already_matched_result = await db.execute(
            select(Media)
            .join(Collection, Collection.media_id == Media.id)
            .where(
                Collection.user_id == current_user.id,
                Media.show_id.isnot(None),
                Media.media_type == MediaType.episode,
                sa_func.lower(Media.tmdb_data["show_title"].astext) == body.show_title.lower(),
            )
            .options(selectinload(Media.show))
            .limit(1)
        )
        already_matched_ep = already_matched_result.scalar_one_or_none()
        if already_matched_ep and already_matched_ep.show:
            target_show = already_matched_ep.show
            # Stamp warnings for shows that were matched before stamping was introduced
            title_lower = body.show_title.lower()
            jobs_res = await db.execute(
                select(SyncJob).where(
                    SyncJob.user_id == current_user.id,
                    SyncJob.status == SyncStatus.completed,
                    SyncJob.warnings.isnot(None),
                )
            )
            for job in jobs_res.scalars().all():
                if not job.warnings:
                    continue
                new_warnings = []
                changed = False
                for w in job.warnings:
                    if w.get("matched"):
                        new_warnings.append(w)
                        continue
                    if (
                        (w.get("series_name") or "").lower() == title_lower
                        or (w.get("title") or "").lower() == title_lower
                    ):
                        new_warnings.append({
                            **w,
                            "matched": True,
                            "matched_tvdb_id": target_show.tvdb_id,
                            "matched_show_id": target_show.tmdb_id,
                            "matched_show_title": target_show.title,
                        })
                        changed = True
                    else:
                        new_warnings.append(w)
                if changed:
                    job.warnings = new_warnings
                    flag_modified(job, "warnings")
            await db.commit()
            return {
                "status": "ok",
                "matched": 0,
                "skipped": 0,
                "tvdb_id": target_show.tvdb_id,
                "show_id": target_show.tmdb_id,
            }

        # Locate stub episodes via source_id recorded in SyncJob warnings.
        # Scan all warnings (stamped or not) — once all episode warnings are stamped,
        # the unmatched-only filter would find nothing and we'd never reach the TVDB/TMDB path.
        title_lower = body.show_title.lower()
        stub_source_ids: list[str] = []
        stub_warn_res = await db.execute(
            select(SyncJob.warnings).where(
                SyncJob.user_id == current_user.id,
                SyncJob.status == SyncStatus.completed,
                SyncJob.warnings.isnot(None),
            ).order_by(SyncJob.created_at.desc())
        )
        for (warnings,) in stub_warn_res.all():
            if not warnings:
                continue
            for w in warnings:
                warn_title = (w.get("series_name") or "").lower()
                if warn_title == title_lower and w.get("source_id"):
                    stub_source_ids.append(str(w["source_id"]))
        if stub_source_ids:
            stub_ep_res = await db.execute(
                select(Media)
                .join(Collection, Collection.media_id == Media.id)
                .join(CollectionFile, CollectionFile.collection_id == Collection.id)
                .where(
                    Collection.user_id == current_user.id,
                    CollectionFile.source_id.in_(stub_source_ids),
                    Media.media_type == MediaType.episode,
                )
                .options(selectinload(Media.show))
                .distinct()
            )
            stub_episodes = stub_ep_res.scalars().all()
            # Use all stub episodes for matching, including any already linked to a stale show.
            episodes = stub_episodes

        if not episodes:
            raise HTTPException(status_code=404, detail="No unmatched episodes found for this show title")

    from collections import defaultdict
    seasons_map: dict[int, list] = defaultdict(list)
    for ep in episodes:
        if ep.season_number is not None:
            seasons_map[ep.season_number].append(ep)

    matched = 0
    skipped = 0
    sem = asyncio.Semaphore(10)

    if body.tvdb_id:
        # ── TVDB path ──────────────────────────────────────────────────────
        from core import tvdb as tvdb_client
        from routers.shows import get_user_tvdb_key

        tvdb_api_key = await get_user_tvdb_key(db, current_user.id)
        if not tvdb_api_key:
            raise HTTPException(status_code=400, detail="TVDB API key required")

        # Find or create Show row keyed by tvdb_id
        target_show_result = await db.execute(select(Show).where(Show.tvdb_id == body.tvdb_id))
        target_show = target_show_result.scalar_one_or_none()
        try:
            raw = await tvdb_client.get_series(body.tvdb_id, tvdb_api_key)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not fetch show from TVDB: {e}")
        show_fmt = tvdb_client.format_series(raw)
        if not target_show:
            target_show = Show(
                tvdb_id=body.tvdb_id,
                tmdb_id=None,
                title=show_fmt["title"] or body.show_title,
                original_title=show_fmt.get("original_title"),
                overview=show_fmt.get("overview"),
                poster_path=show_fmt.get("poster_path"),
                backdrop_path=show_fmt.get("backdrop_path"),
                status=show_fmt.get("status"),
                first_air_date=show_fmt.get("first_air_date"),
                last_air_date=show_fmt.get("last_air_date"),
                tmdb_data={"seasons": show_fmt.get("seasons", []), "genres": show_fmt.get("genres", []), "source": "tvdb"},
            )
            db.add(target_show)
            await db.flush()
        else:
            target_show.title = show_fmt["title"] or body.show_title or target_show.title
            target_show.original_title = show_fmt.get("original_title") or target_show.original_title
            target_show.overview = show_fmt.get("overview") or target_show.overview
            target_show.poster_path = show_fmt.get("poster_path") or target_show.poster_path
            target_show.backdrop_path = show_fmt.get("backdrop_path") or target_show.backdrop_path
            target_show.status = show_fmt.get("status") or target_show.status
            target_show.first_air_date = show_fmt.get("first_air_date") or target_show.first_air_date
            target_show.last_air_date = show_fmt.get("last_air_date") or target_show.last_air_date
            target_show.tmdb_data = {"seasons": show_fmt.get("seasons", []), "genres": show_fmt.get("genres", []), "source": "tvdb"}

        async def _enrich_season_tvdb(season_number: int, season_episodes: list) -> None:
            nonlocal matched, skipped
            async with sem:
                try:
                    raw_eps = await tvdb_client.get_series_episodes(body.tvdb_id, season_number, tvdb_api_key)
                except Exception:
                    for media in season_episodes:
                        media.show_id = target_show.id
                    skipped += len(season_episodes)
                    return
                ep_map = {e.get("number"): e for e in raw_eps}
                for media in season_episodes:
                    media.show_id = target_show.id
                    ep = ep_map.get(media.episode_number)
                    if ep:
                        tvdb_ep_id = ep.get("id")
                        # Store TVDB episode ID in tmdb_id column for ActionBar compatibility
                        if tvdb_ep_id:
                            media.tmdb_id = tvdb_ep_id
                        media.title = ep.get("name") or media.title
                        media.overview = ep.get("overview")
                        if ep.get("image"):
                            media.poster_path = tvdb_client._image_url(ep["image"])
                        media.release_date = ep.get("aired")
                        media.tmdb_data = {**(media.tmdb_data or {}), "runtime": ep.get("runtime"), "tvdb_episode_id": tvdb_ep_id, "source": "tvdb"}
                        matched += 1
                    else:
                        skipped += 1

        await asyncio.gather(*[_enrich_season_tvdb(sn, eps) for sn, eps in seasons_map.items()])

    else:
        # ── TMDB path (original behaviour) ────────────────────────────────
        settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
        settings = settings_result.scalar_one_or_none()
        tmdb_api_key = await _get_effective_tmdb_key(db, settings)
        if not tmdb_api_key:
            raise HTTPException(status_code=400, detail="TMDB API key required")

        try:
            show_data = await tmdb.get_show(body.tmdb_id, api_key=tmdb_api_key)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not fetch show from TMDB: {e}")
        seasons_meta = [
            {
                "season_number": s["season_number"],
                "name": s.get("name"),
                "overview": s.get("overview"),
                "poster_path": tmdb.poster_url(s.get("poster_path")),
                "episode_count": s.get("episode_count"),
                "air_date": s.get("air_date"),
            }
            for s in show_data.get("seasons", [])
        ]

        # Prefer an existing show that shares the TVDB cross-reference from TMDB external_ids.
        # This consolidates TMDB matches with auto-matched TVDB shows so both versions of
        # the same Plex show (e.g. color + B&W) end up on the same show page.
        tmdb_tvdb_id = (show_data.get("external_ids") or {}).get("tvdb_id")
        target_show = None
        if tmdb_tvdb_id:
            tvdb_cross_result = await db.execute(select(Show).where(Show.tvdb_id == tmdb_tvdb_id))
            target_show = tvdb_cross_result.scalar_one_or_none()

        if target_show is not None:
            # Found a TVDB-matched show to consolidate with.
            # Clear tmdb_id from any stale TMDB-only show that previously claimed this TMDB ID,
            # then re-home its episodes to target_show so they aren't orphaned.
            if target_show.tmdb_id != body.tmdb_id:
                displaced_result = await db.execute(select(Show).where(Show.tmdb_id == body.tmdb_id))
                displaced_show = displaced_result.scalar_one_or_none()
                if displaced_show and displaced_show.id != target_show.id:
                    await db.execute(
                        update(Media).where(Media.show_id == displaced_show.id).values(show_id=target_show.id)
                    )
                    displaced_show.tmdb_id = None
            target_show.tmdb_id = body.tmdb_id
        else:
            tmdb_show_result = await db.execute(select(Show).where(Show.tmdb_id == body.tmdb_id))
            target_show = tmdb_show_result.scalar_one_or_none()

        if not target_show:
            target_show = Show(
                tmdb_id=body.tmdb_id,
                title=show_data.get("name") or show_data.get("original_name"),
                original_title=show_data.get("original_name"),
                overview=show_data.get("overview"),
                poster_path=tmdb.poster_url(show_data.get("poster_path")),
                backdrop_path=tmdb.poster_url(show_data.get("backdrop_path"), size="w1280"),
                tmdb_rating=show_data.get("vote_average"),
                status=show_data.get("status"),
                tagline=show_data.get("tagline"),
                first_air_date=show_data.get("first_air_date"),
                last_air_date=show_data.get("last_air_date"),
                tmdb_data={**show_data, "seasons": seasons_meta, "genres": [g["name"] if isinstance(g, dict) else g for g in show_data.get("genres", [])]},
            )
            db.add(target_show)
            await db.flush()
        else:
            # Refresh metadata in case it has stale data from a previous wrong match.
            target_show.title = show_data.get("name") or show_data.get("original_name") or target_show.title
            target_show.original_title = show_data.get("original_name") or target_show.original_title
            target_show.overview = show_data.get("overview") or target_show.overview
            target_show.poster_path = tmdb.poster_url(show_data.get("poster_path")) or target_show.poster_path
            target_show.backdrop_path = tmdb.poster_url(show_data.get("backdrop_path"), size="w1280") or target_show.backdrop_path
            target_show.tmdb_rating = show_data.get("vote_average") or target_show.tmdb_rating
            target_show.status = show_data.get("status") or target_show.status
            target_show.tagline = show_data.get("tagline") or target_show.tagline
            target_show.first_air_date = show_data.get("first_air_date") or target_show.first_air_date
            target_show.last_air_date = show_data.get("last_air_date") or target_show.last_air_date
            target_show.tmdb_data = {**show_data, "seasons": seasons_meta}

        async def _enrich_season(season_number: int, season_episodes: list) -> None:
            nonlocal matched, skipped
            async with sem:
                try:
                    season_data = await tmdb.get_season(body.tmdb_id, season_number, api_key=tmdb_api_key)
                except Exception:
                    skipped += len(season_episodes)
                    return
                ep_map = {ep["episode_number"]: ep for ep in season_data.get("episodes", [])}
                for media in season_episodes:
                    media.show_id = target_show.id
                    ep = ep_map.get(media.episode_number)
                    if ep:
                        media.tmdb_id = ep.get("id") or media.tmdb_id
                        media.title = ep.get("name") or media.title
                        media.overview = ep.get("overview")
                        media.poster_path = tmdb.poster_url(ep.get("still_path"), size="w500")
                        media.release_date = ep.get("air_date")
                        media.tmdb_rating = ep.get("vote_average")
                        media.tmdb_data = {"runtime": ep.get("runtime"), "cast": []}
                        matched += 1
                    else:
                        media.show_id = target_show.id
                        skipped += 1

        await asyncio.gather(*[_enrich_season(sn, eps) for sn, eps in seasons_map.items()])

    # Stamp the matched state into all relevant SyncJob warnings so the panel
    # reflects the match immediately without a re-sync.
    title_lower = body.show_title.lower()
    jobs_res = await db.execute(
        select(SyncJob).where(
            SyncJob.user_id == current_user.id,
            SyncJob.status == SyncStatus.completed,
            SyncJob.warnings.isnot(None),
        )
    )
    for job in jobs_res.scalars().all():
        if not job.warnings:
            continue
        new_warnings = []
        changed = False
        for w in job.warnings:
            if (
                (w.get("series_name") or "").lower() == title_lower
                or (w.get("title") or "").lower() == title_lower
            ):
                new_warnings.append({
                    **w,
                    "matched": True,
                    "matched_tvdb_id": body.tvdb_id,
                    "matched_show_id": target_show.tmdb_id if target_show else None,
                    "matched_show_title": target_show.title if target_show else None,
                })
                changed = True
            else:
                new_warnings.append(w)
        if changed:
            job.warnings = new_warnings
            flag_modified(job, "warnings")
    await db.commit()

    return {
        "status": "ok",
        "matched": matched,
        "skipped": skipped,
        "tvdb_id": body.tvdb_id,
        "show_id": target_show.tmdb_id if target_show else None,
    }


@router.post("/heal-stub-show-titles")
async def heal_stub_show_titles(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Backfill tmdb_data['show_title'] for stub episodes that have it NULL,
    using series_name from SyncJob warnings matched via CollectionFile source_id."""
    warn_res = await db.execute(
        select(SyncJob.warnings).where(
            SyncJob.user_id == current_user.id,
            SyncJob.warnings.isnot(None),
        )
    )
    # Build source_id → series_name map from all warnings
    source_to_title: dict[str, str] = {}
    for (warnings,) in warn_res.all():
        for w in (warnings or []):
            sn = w.get("series_name")
            sid = w.get("source_id")
            if sn and sid:
                source_to_title[str(sid)] = sn

    if not source_to_title:
        return {"status": "ok", "healed": 0}

    # Find stub episodes with NULL tmdb_data via those source_ids
    ep_res = await db.execute(
        select(Media, CollectionFile.source_id)
        .join(Collection, Collection.media_id == Media.id)
        .join(CollectionFile, CollectionFile.collection_id == Collection.id)
        .where(
            Collection.user_id == current_user.id,
            Media.media_type == MediaType.episode,
            Media.tmdb_data.is_(None),
            CollectionFile.source_id.in_(list(source_to_title.keys())),
        )
        .distinct()
    )
    healed = 0
    for media, source_id in ep_res.all():
        title = source_to_title.get(source_id)
        if title:
            media.tmdb_data = {"show_title": title}
            healed += 1

    await db.commit()
    return {"status": "ok", "healed": healed}


class UnmatchShowBody(BaseModel):
    show_title: str


@router.post("/unmatch-show")
async def unmatch_show(
    body: UnmatchShowBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Unlink stub episodes from their manually-matched Show row so they can be re-matched."""
    from sqlalchemy import func as sa_func

    ep_result = await db.execute(
        select(Media)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.media_type == MediaType.episode,
            Media.show_id.isnot(None),
            sa_func.lower(Media.tmdb_data["show_title"].astext) == body.show_title.lower(),
        )
        .distinct()
    )
    episodes = ep_result.scalars().all()
    if not episodes:
        # Fallback: find via source_ids from SyncJob warnings (tmdb_data may be NULL)
        title_lower_u = body.show_title.lower()
        src_warn_res = await db.execute(
            select(SyncJob.warnings).where(
                SyncJob.user_id == current_user.id,
                SyncJob.warnings.isnot(None),
            )
        )
        fallback_source_ids: list[str] = []
        for (warnings,) in src_warn_res.all():
            for w in (warnings or []):
                if (w.get("series_name") or w.get("title") or "").lower() == title_lower_u and w.get("source_id"):
                    fallback_source_ids.append(str(w["source_id"]))
        if fallback_source_ids:
            fb_res = await db.execute(
                select(Media)
                .join(Collection, Collection.media_id == Media.id)
                .join(CollectionFile, CollectionFile.collection_id == Collection.id)
                .where(
                    Collection.user_id == current_user.id,
                    Media.media_type == MediaType.episode,
                    CollectionFile.source_id.in_(fallback_source_ids),
                )
                .distinct()
            )
            episodes = fb_res.scalars().all()
    if not episodes:
        raise HTTPException(status_code=404, detail="No matched stub episodes found for this show title")

    show_ids_to_check: set[int] = set()
    for ep in episodes:
        if ep.show_id:
            show_ids_to_check.add(ep.show_id)
        ep.show_id = None
        ep.tmdb_id = None
        ep.overview = None
        ep.poster_path = None
        ep.release_date = None
        ep.tmdb_rating = None

    await db.commit()

    # Remove Show rows that are now orphaned (no remaining linked media).
    # TMDB-only shows (no tvdb_id) are deleted so a future match creates a fresh row
    # instead of reusing a show row that may have stale/wrong metadata.
    # TVDB-tagged shows are kept — they carry a canonical TVDB ID used elsewhere.
    for show_id in show_ids_to_check:
        remaining = await db.execute(
            select(func.count()).select_from(Media).where(Media.show_id == show_id)
        )
        if remaining.scalar_one() == 0:
            show_q = await db.execute(
                select(Show).where(Show.id == show_id, Show.tvdb_id.is_(None))
            )
            orphaned = show_q.scalar_one_or_none()
            if orphaned:
                await db.delete(orphaned)

    # Clear matched stamps from SyncJob warnings
    title_lower = body.show_title.lower()
    jobs_res = await db.execute(
        select(SyncJob).where(
            SyncJob.user_id == current_user.id,
            SyncJob.status == SyncStatus.completed,
            SyncJob.warnings.isnot(None),
        )
    )
    for job in jobs_res.scalars().all():
        if not job.warnings:
            continue
        new_warnings = []
        changed = False
        for w in job.warnings:
            if w.get("matched") and (
                (w.get("series_name") or "").lower() == title_lower
                or (w.get("title") or "").lower() == title_lower
            ):
                cleared = {k: v for k, v in w.items() if not k.startswith("matched")}
                new_warnings.append(cleared)
                changed = True
            else:
                new_warnings.append(w)
        if changed:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job.id).values(warnings=new_warnings)
            )
    await db.commit()

    return {"status": "ok", "unmatched": len(episodes)}


# ── Unmatched movie matching ──────────────────────────────────────────────────

class MatchUnmatchedMovieBody(BaseModel):
    movie_title: str
    tmdb_id: int


@router.post("/match-unmatched-movie")
async def match_unmatched_movie(
    body: MatchUnmatchedMovieBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Link unmatched local movies (no tmdb_id) to a TMDB movie."""
    from sqlalchemy import func as sa_func

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    tmdb_api_key = await _get_effective_tmdb_key(db, settings)
    if not tmdb_api_key:
        raise HTTPException(status_code=400, detail="TMDB API key required")

    movie_result = await db.execute(
        select(Media)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.tmdb_id.is_(None),
            Media.media_type == MediaType.movie,
            sa_func.lower(Media.title) == body.movie_title.lower(),
        )
        .distinct()
    )
    movies = movie_result.scalars().all()
    if not movies:
        raise HTTPException(status_code=404, detail="No unmatched movies found for this title")

    # Fetch TMDB metadata once to get the canonical title
    try:
        movie_data = await tmdb.get_movie(body.tmdb_id, api_key=tmdb_api_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch movie from TMDB: {e}")

    matched_title = movie_data.get("title") or body.movie_title
    for media in movies:
        media.tmdb_id = body.tmdb_id
        await enrich_media(media, api_key=tmdb_api_key)

    # Stamp the matched state into all relevant SyncJob warnings
    title_lower = body.movie_title.lower()
    jobs_res = await db.execute(
        select(SyncJob).where(
            SyncJob.user_id == current_user.id,
            SyncJob.status == SyncStatus.completed,
            SyncJob.warnings.isnot(None),
        )
    )
    for job in jobs_res.scalars().all():
        if not job.warnings:
            continue
        new_warnings = []
        changed = False
        for w in job.warnings:
            if (
                w.get("media_type") == "movie"
                and not w.get("matched")
                and (w.get("title") or "").lower() == title_lower
            ):
                new_warnings.append({
                    **w,
                    "matched": True,
                    "matched_tmdb_id": body.tmdb_id,
                    "matched_movie_title": matched_title,
                })
                changed = True
            else:
                new_warnings.append(w)
        if changed:
            job.warnings = new_warnings
            flag_modified(job, "warnings")

    await db.commit()
    return {"status": "ok", "matched": len(movies), "tmdb_id": body.tmdb_id}


class UnmatchMovieBody(BaseModel):
    movie_title: str


@router.post("/unmatch-movie")
async def unmatch_movie(
    body: UnmatchMovieBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear TMDB link from locally-matched movies so they can be re-matched."""
    from sqlalchemy import func as sa_func

    movie_result = await db.execute(
        select(Media)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.media_type == MediaType.movie,
            Media.tmdb_id.isnot(None),
            sa_func.lower(Media.title) == body.movie_title.lower(),
        )
        .distinct()
    )
    movies = movie_result.scalars().all()
    if not movies:
        raise HTTPException(status_code=404, detail="No matched movies found for this title")

    for media in movies:
        media.tmdb_id = None
        media.overview = None
        media.poster_path = None
        media.backdrop_path = None
        media.release_date = None
        media.tmdb_rating = None
        media.tmdb_data = None

    # Clear matched stamps from SyncJob warnings
    title_lower = body.movie_title.lower()
    jobs_res = await db.execute(
        select(SyncJob).where(
            SyncJob.user_id == current_user.id,
            SyncJob.status == SyncStatus.completed,
            SyncJob.warnings.isnot(None),
        )
    )
    for job in jobs_res.scalars().all():
        if not job.warnings:
            continue
        new_warnings = []
        changed = False
        for w in job.warnings:
            if (
                w.get("matched")
                and w.get("media_type") == "movie"
                and (w.get("title") or "").lower() == title_lower
            ):
                cleared = {k: v for k, v in w.items() if not k.startswith("matched")}
                new_warnings.append(cleared)
                changed = True
            else:
                new_warnings.append(w)
        if changed:
            job.warnings = new_warnings
            flag_modified(job, "warnings")

    await db.commit()
    return {"status": "ok", "unmatched": len(movies)}


@router.get("/matched-shows")
async def list_matched_shows(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all matched shows (TMDB or TVDB) for the current user.

    Used by the settings panel to overlay matched state onto SyncJob warnings that
    were stamped before the auto-stamping logic existed, without requiring a resync.

    Two sources are combined:
    1. Media rows with show_id → matched Show, keyed by tmdb_data["show_title"].
    2. SyncJob warnings with matched:true (covers shows where show_title is absent from
       tmdb_data, e.g. episodes that weren't created as stubs or had tmdb_data overwritten).
    """
    from sqlalchemy import func as sa_func

    # Fetch all shows in the system to build a db_id -> Show mapping
    # This allows us to map any database show.id to its tmdb_id dynamically,
    # correcting any legacy/already-stamped warning entries where matched_show_id was show.id.
    shows_res = await db.execute(select(Show.id, Show.tmdb_id, Show.tvdb_id, Show.title))
    show_id_map = {
        row.id: {
            "tmdb_id": row.tmdb_id,
            "tvdb_id": row.tvdb_id,
            "title": row.title,
        }
        for row in shows_res.all()
    }

    seen: dict[str, dict] = {}

    # Source 1: episodes linked to matched shows (TMDB or TVDB), keyed by show_title in tmdb_data
    result = await db.execute(
        select(
            Media.tmdb_data["show_title"].astext.label("show_title"),
            Show.tmdb_id.label("show_id"),
            Show.tvdb_id,
            Show.title.label("show_title_matched"),
        )
        .join(Collection, Collection.media_id == Media.id)
        .join(Show, Show.id == Media.show_id)
        .where(
            Collection.user_id == current_user.id,
            Media.media_type == MediaType.episode,
            Media.show_id.isnot(None),
            (Show.tvdb_id.isnot(None) | Show.tmdb_id.isnot(None)),
            Media.tmdb_data["show_title"].astext.isnot(None),
        )
        .distinct()
    )
    for row in result.all():
        key = (row.show_title or "").lower()
        if key and key not in seen:
            seen[key] = {
                "show_title": row.show_title,
                "show_id": row.show_id,
                "tvdb_id": row.tvdb_id,
                "show_title_matched": row.show_title_matched,
            }

    # Source 2: SyncJob warnings stamped with matched:true (fallback for missing show_title)
    jobs_res = await db.execute(
        select(SyncJob.warnings).where(
            SyncJob.user_id == current_user.id,
            SyncJob.status == SyncStatus.completed,
            SyncJob.warnings.isnot(None),
        ).order_by(SyncJob.created_at.desc()).limit(10)
    )
    for (warnings,) in jobs_res.all():
        if not warnings:
            continue
        for w in warnings:
            if not w.get("matched"):
                continue
            title = w.get("series_name") or w.get("title")
            if not title:
                continue
            key = title.lower()
            if key not in seen:
                legacy_show_id = w.get("matched_show_id")
                matched_tvdb_id = w.get("matched_tvdb_id")
                matched_show_id = legacy_show_id

                if legacy_show_id in show_id_map:
                    show_info = show_id_map[legacy_show_id]
                    # Only map if titles match (case-insensitive) or tmdb_id matches, preventing collisions
                    db_title = show_info["title"].lower()
                    matched_title = (w.get("matched_show_title") or "").lower()
                    if db_title == key or db_title == matched_title or show_info["tmdb_id"] == legacy_show_id:
                        matched_show_id = show_info["tmdb_id"]
                        if show_info["tvdb_id"]:
                            matched_tvdb_id = show_info["tvdb_id"]

                seen[key] = {
                    "show_title": title,
                    "show_id": matched_show_id,
                    "tvdb_id": matched_tvdb_id,
                    "show_title_matched": w.get("matched_show_title"),
                }

    return list(seen.values())
