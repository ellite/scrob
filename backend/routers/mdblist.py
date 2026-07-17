"""MDBList cloud synchronization endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dt_parser
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import mdblist as mdblist_client
from core.enrichment import enrich_media
from db import engine, get_db
from dependencies import get_current_user
from models.base import CollectionSource, MediaType
from models.events import WatchEvent
from models.lists import List as ListModel, ListItem
from models.media import Media
from models.ratings import Rating
from models.show import Show
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings
from routers.trakt import (
    _get_or_create_episode_media,
    _get_or_create_movie_media,
    _get_or_create_show,
)

logger = logging.getLogger(__name__)
router = APIRouter()
WATCHLIST_SLUG = "__watchlist__"


def _utc_naive(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = dt_parser.isoparse(value)
        except (TypeError, ValueError):
            return datetime.utcnow()
    else:
        return datetime.utcnow()
    if parsed.tzinfo:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso_utc(value: datetime | None) -> str:
    value = value or datetime.utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _entry_data(kind: str, entry: dict[str, Any]) -> dict[str, Any]:
    singular = {"movies": "movie", "shows": "show", "seasons": "season", "episodes": "episode"}[kind]
    nested = entry.get(singular)
    return nested if isinstance(nested, dict) else entry


def _tmdb_id(data: dict[str, Any]) -> int | None:
    ids = data.get("ids")
    ids = ids if isinstance(ids, dict) else {}
    return _integer(ids.get("tmdb") or data.get("tmdb_id"))

async def _resolve_external_tmdb_id(
    data: dict[str, Any],
    media_type: str,
    api_key: str | None,
    cache: dict[tuple[str, str], int | None],
) -> int | None:
    direct_id = _tmdb_id(data)
    if direct_id:
        return direct_id

    ids = data.get("ids")
    ids = ids if isinstance(ids, dict) else {}
    from core import tmdb

    for provider, external_source in (("imdb", "imdb_id"), ("tvdb", "tvdb_id")):
        external_id = ids.get(provider) or data.get(f"{provider}_id")
        if external_id is None:
            continue
        cache_key = (external_source, str(external_id))
        if cache_key in cache:
            return cache[cache_key]
        try:
            result = await tmdb.find_by_external_id(
                str(external_id),
                external_source,
                api_key=api_key,
            )
            result_key = "movie_results" if media_type == "movie" else "tv_results"
            matches = result.get(result_key) or []
            resolved = _integer(matches[0].get("id")) if matches else None
        except Exception as exc:
            logger.warning(
                "Could not resolve MDBList %s=%s through TMDB: %s",
                provider,
                external_id,
                exc,
            )
            resolved = None
        cache[cache_key] = resolved
        if resolved:
            return resolved
    return None




def _episode_identity(entry: dict[str, Any]) -> tuple[int | None, int | None, int | None, str]:
    episode = _entry_data("episodes", entry)
    show_data = entry.get("show") or episode.get("show") or {}
    show_data = show_data if isinstance(show_data, dict) else {}
    show_tmdb_id = _tmdb_id(show_data)
    ids = episode.get("ids") if isinstance(episode.get("ids"), dict) else {}
    show_tmdb_id = show_tmdb_id or _integer(ids.get("show_tmdb") or episode.get("show_tmdb_id"))

    season = episode.get("season", entry.get("season"))
    if isinstance(season, dict):
        season = season.get("number")
    episode_number = episode.get("number", episode.get("episode", entry.get("episode")))
    title = str(episode.get("title") or episode.get("name") or "")
    return show_tmdb_id, _integer(season), _integer(episode_number), title


async def _get_or_create_series_media(
    db: AsyncSession,
    tmdb_id: int,
    title: str,
    api_key: str | None,
) -> Media | None:
    result = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.series)
    )
    media = result.scalars().first()
    if media:
        return media

    from core import tmdb

    try:
        data = await tmdb.get_show(tmdb_id, api_key=api_key)
        media = Media(
            tmdb_id=tmdb_id,
            media_type=MediaType.series,
            title=data.get("name") or title,
        )
        db.add(media)
        await db.flush()
        await enrich_media(media, api_key=api_key)
        return media
    except Exception as exc:
        logger.warning("Could not fetch MDBList show tmdb=%s: %s", tmdb_id, exc)
        return None


async def _resolve_media(
    db: AsyncSession,
    kind: str,
    entry: dict[str, Any],
    api_key: str | None,
    external_cache: dict[tuple[str, str], int | None],
) -> Media | None:
    data = _entry_data(kind, entry)
    title = str(data.get("title") or data.get("name") or "")
    if kind == "movies":
        tmdb_id = await _resolve_external_tmdb_id(data, "movie", api_key, external_cache)
        return await _get_or_create_movie_media(db, tmdb_id, title, api_key) if tmdb_id else None
    if kind == "shows":
        tmdb_id = await _resolve_external_tmdb_id(data, "tv", api_key, external_cache)
        return await _get_or_create_series_media(db, tmdb_id, title, api_key) if tmdb_id else None
    if kind == "episodes":
        show_tmdb_id, season, episode, _ = _episode_identity(entry)
        if show_tmdb_id is None:
            episode_data = _entry_data("episodes", entry)
            show_data = entry.get("show") or episode_data.get("show") or {}
            if isinstance(show_data, dict):
                show_tmdb_id = await _resolve_external_tmdb_id(
                    show_data, "tv", api_key, external_cache
                )
        if show_tmdb_id is None or season is None or episode is None:
            return None
        show = await _get_or_create_show(db, show_tmdb_id, "", api_key)
        if not show:
            return None
        return await _get_or_create_episode_media(
            db, show.id, show_tmdb_id, season, episode, api_key
        )
    return None


def _empty_payload() -> dict[str, list[dict[str, Any]]]:
    return {"movies": [], "shows": [], "seasons": [], "episodes": []}


def _payload_item(
    media: Media,
    *,
    watched_at: datetime | None = None,
    rating: float | None = None,
    rated_at: datetime | None = None,
) -> tuple[str, dict[str, Any]] | None:
    if not media.tmdb_id:
        return None
    if media.media_type == MediaType.movie:
        kind = "movies"
    elif media.media_type == MediaType.series:
        kind = "shows"
    elif media.media_type == MediaType.episode:
        kind = "episodes"
    else:
        return None

    item: dict[str, Any] = {"ids": {"tmdb": media.tmdb_id}}
    if watched_at is not None:
        item["watched_at"] = _iso_utc(watched_at)
    if rating is not None:
        item["rating"] = float(rating)
        item["rated_at"] = _iso_utc(rated_at or datetime.now(timezone.utc))
    return kind, item


async def _effective_tmdb_key(db: AsyncSession, settings: UserSettings) -> str | None:
    from models.global_settings import GlobalSettings

    if settings.tmdb_api_key:
        return settings.tmdb_api_key
    result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    global_settings = result.scalar_one_or_none()
    return global_settings.tmdb_api_key if global_settings else None


async def _import_watched(
    db: AsyncSession,
    user_id: int,
    payload: dict[str, Any],
    api_key: str | None,
    external_cache: dict[tuple[str, str], int | None],
    stats: dict[str, int],
) -> set[int]:
    existing_result = await db.execute(
        select(WatchEvent.media_id).where(
            WatchEvent.user_id == user_id,
            WatchEvent.completed.is_(True),
        )
    )
    existing = {row[0] for row in existing_result.all()}
    changed: set[int] = set()

    for kind in ("movies", "shows", "episodes"):
        for entry in payload.get(kind, []):
            try:
                async with db.begin_nested():
                    media = await _resolve_media(db, kind, entry, api_key, external_cache)
                    if not media:
                        stats["skipped"] += 1
                        continue
                    if media.id in existing:
                        stats["skipped"] += 1
                        continue
                    watched_at = entry.get("watched_at") or entry.get("last_watched_at")
                    db.add(
                        WatchEvent(
                            user_id=user_id,
                            media_id=media.id,
                            watched_at=_utc_naive(watched_at),
                            completed=True,
                            play_count=max(_integer(entry.get("plays")) or 1, 1),
                        )
                    )
                    existing.add(media.id)
                    changed.add(media.id)
                    stats["watched"] += 1
            except Exception as exc:
                logger.warning("Error importing MDBList %s watch item: %s", kind, exc)
                stats["errors"] += 1

    stats["skipped"] += len(payload.get("seasons", []))
    return changed


async def _import_ratings(
    db: AsyncSession,
    user_id: int,
    payload: dict[str, Any],
    api_key: str | None,
    external_cache: dict[tuple[str, str], int | None],
    stats: dict[str, int],
) -> dict[int, float]:
    ratings_result = await db.execute(
        select(Rating).where(Rating.user_id == user_id, Rating.season_number.is_(None))
    )
    existing = {rating.media_id: rating for rating in ratings_result.scalars().all()}
    changed: dict[int, float] = {}

    for kind in ("movies", "shows", "episodes"):
        for entry in payload.get(kind, []):
            rating_value = entry.get("rating")
            try:
                rating = float(rating_value)
            except (TypeError, ValueError):
                stats["skipped"] += 1
                continue
            try:
                async with db.begin_nested():
                    media = await _resolve_media(db, kind, entry, api_key, external_cache)
                    if not media:
                        stats["skipped"] += 1
                        continue
                    current = existing.get(media.id)
                    if current and current.rating == rating:
                        stats["skipped"] += 1
                        continue
                    if current:
                        current.rating = rating
                        current.rated_at = _utc_naive(entry.get("rated_at"))
                    else:
                        current = Rating(
                            user_id=user_id,
                            media_id=media.id,
                            rating=rating,
                            rated_at=_utc_naive(entry.get("rated_at")),
                        )
                        db.add(current)
                        existing[media.id] = current
                    changed[media.id] = rating
                    stats["ratings"] += 1
            except Exception as exc:
                logger.warning("Error importing MDBList %s rating: %s", kind, exc)
                stats["errors"] += 1

    stats["skipped"] += len(payload.get("seasons", []))
    return changed


async def _import_watchlist(
    db: AsyncSession,
    user_id: int,
    payload: dict[str, Any],
    api_key: str | None,
    external_cache: dict[tuple[str, str], int | None],
    stats: dict[str, int],
) -> None:
    list_result = await db.execute(
        select(ListModel).where(
            ListModel.user_id == user_id,
            ListModel.mdblist_slug == WATCHLIST_SLUG,
        )
    )
    watchlist = list_result.scalar_one_or_none()
    if not watchlist:
        watchlist = ListModel(
            user_id=user_id,
            name="MDBList - Watchlist",
            mdblist_slug=WATCHLIST_SLUG,
        )
        db.add(watchlist)
        await db.flush()
        stats["lists"] += 1

    existing_result = await db.execute(
        select(ListItem.media_id).where(ListItem.list_id == watchlist.id)
    )
    existing = {row[0] for row in existing_result.all()}
    remote_ids: set[int] = set()

    for kind in ("movies", "shows"):
        for entry in payload.get(kind, []):
            try:
                async with db.begin_nested():
                    media = await _resolve_media(db, kind, entry, api_key, external_cache)
                    if not media:
                        stats["skipped"] += 1
                        continue
                    remote_ids.add(media.id)
                    if media.id not in existing:
                        db.add(ListItem(list_id=watchlist.id, media_id=media.id))
                        existing.add(media.id)
                        stats["watchlist_added"] += 1
            except Exception as exc:
                logger.warning("Error importing MDBList watchlist %s: %s", kind, exc)
                stats["errors"] += 1

    stale = existing - remote_ids
    if stale:
        await db.execute(
            delete(ListItem).where(
                ListItem.list_id == watchlist.id,
                ListItem.media_id.in_(stale),
            )
        )
        stats["watchlist_removed"] += len(stale)


async def run_mdblist_sync(user_id: int, job_id: int) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as db:
        try:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running)
            )
            await db.commit()

            settings_result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            settings = settings_result.scalar_one_or_none()
            if not settings or not settings.mdblist_api_key:
                raise RuntimeError("MDBList API key is not configured")

            requests = []
            labels = []
            if settings.mdblist_sync_watched:
                labels.append("watched")
                requests.append(mdblist_client.get_watched(settings.mdblist_api_key))
            if settings.mdblist_sync_ratings:
                labels.append("ratings")
                requests.append(mdblist_client.get_ratings(settings.mdblist_api_key))
            if settings.mdblist_sync_watchlist:
                labels.append("watchlist")
                requests.append(mdblist_client.get_watchlist(settings.mdblist_api_key))

            import asyncio

            responses = await asyncio.gather(*requests)
            snapshots = dict(zip(labels, responses, strict=True))
            total_items = sum(
                len(values)
                for snapshot in snapshots.values()
                for values in snapshot.values()
                if isinstance(values, list)
            )
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_items)
            )
            await db.commit()

            tmdb_key = await _effective_tmdb_key(db, settings)
            stats = {
                "watched": 0,
                "ratings": 0,
                "lists": 0,
                "watchlist_added": 0,
                "watchlist_removed": 0,
                "skipped": 0,
                "errors": 0,
            }
            new_watched: set[int] = set()
            new_ratings: dict[int, float] = {}
            external_cache: dict[tuple[str, str], int | None] = {}

            if "watched" in snapshots:
                new_watched = await _import_watched(
                    db, user_id, snapshots["watched"], tmdb_key, external_cache, stats
                )
            if "ratings" in snapshots:
                new_ratings = await _import_ratings(
                    db, user_id, snapshots["ratings"], tmdb_key, external_cache, stats
                )
            if "watchlist" in snapshots:
                await _import_watchlist(
                    db, user_id, snapshots["watchlist"], tmdb_key, external_cache, stats
                )
            await db.commit()

            from routers.sync import _fan_out_changes_to_other_connections

            await _fan_out_changes_to_other_connections(
                db,
                user_id,
                None,
                new_watched,
                new_ratings,
                settings=settings,
                exclude_cloud_source=CollectionSource.mdblist,
            )
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    processed_items=total_items,
                    errors=stats["errors"],
                    stats=stats,
                )
            )
            await db.commit()
        except Exception as exc:
            logger.exception("MDBList pull job %s failed", job_id)
            await db.rollback()
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.failed,
                    error_message=str(exc),
                )
            )
            await db.commit()


async def _load_payload_media(db: AsyncSession, media_ids: set[int]) -> dict[int, Media]:
    if not media_ids:
        return {}
    from routers.sync import _select_in_chunks

    media = await _select_in_chunks(
        db,
        lambda chunk: select(Media).where(Media.id.in_(chunk)),
        list(media_ids),
    )
    return {item.id: item for item in media}


async def run_mdblist_push(user_id: int, job_id: int) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as db:
        try:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running)
            )
            await db.commit()

            settings_result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            settings = settings_result.scalar_one_or_none()
            if not settings or not settings.mdblist_api_key:
                raise RuntimeError("MDBList API key is not configured")

            watched_rows: list[tuple[int, datetime]] = []
            rating_rows: list[tuple[int, float, datetime | None]] = []
            watchlist_ids: set[int] = set()

            if settings.mdblist_push_watched:
                watched_result = await db.execute(
                    select(WatchEvent.media_id, func.max(WatchEvent.watched_at))
                    .where(WatchEvent.user_id == user_id, WatchEvent.completed.is_(True))
                    .group_by(WatchEvent.media_id)
                )
                watched_rows = list(watched_result.all())
            if settings.mdblist_push_ratings:
                ratings_result = await db.execute(
                    select(Rating.media_id, Rating.rating, Rating.rated_at).where(
                        Rating.user_id == user_id,
                        Rating.rating.isnot(None),
                        Rating.season_number.is_(None),
                    )
                )
                rating_rows = [
                    (media_id, float(rating), rated_at)
                    for media_id, rating, rated_at in ratings_result.all()
                ]
            if settings.mdblist_push_watchlist:
                watchlist_result = await db.execute(
                    select(ListModel).where(
                        ListModel.user_id == user_id,
                        ListModel.mdblist_slug == WATCHLIST_SLUG,
                    )
                )
                watchlist = watchlist_result.scalar_one_or_none()
                if watchlist:
                    item_result = await db.execute(
                        select(ListItem.media_id).where(ListItem.list_id == watchlist.id)
                    )
                    watchlist_ids = {row[0] for row in item_result.all()}

            all_ids = {row[0] for row in watched_rows} | {row[0] for row in rating_rows} | watchlist_ids
            media_by_id = await _load_payload_media(db, all_ids)
            watched_payload = _empty_payload()
            ratings_payload = _empty_payload()
            watchlist_payload = _empty_payload()

            for media_id, watched_at in watched_rows:
                media = media_by_id.get(media_id)
                item = _payload_item(media, watched_at=watched_at) if media else None
                if item:
                    watched_payload[item[0]].append(item[1])
            for media_id, rating, rated_at in rating_rows:
                media = media_by_id.get(media_id)
                item = _payload_item(media, rating=rating, rated_at=rated_at) if media else None
                if item:
                    ratings_payload[item[0]].append(item[1])
            for media_id in watchlist_ids:
                media = media_by_id.get(media_id)
                item = _payload_item(media) if media else None
                if item and item[0] in ("movies", "shows"):
                    watchlist_payload[item[0]].append(item[1])

            total_items = sum(
                len(values)
                for payload in (watched_payload, ratings_payload, watchlist_payload)
                for values in payload.values()
            )
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_items)
            )
            await db.commit()

            results: dict[str, Any] = {}
            if settings.mdblist_push_watched:
                results["watched"] = await mdblist_client.push_watched(
                    settings.mdblist_api_key, watched_payload
                )
            if settings.mdblist_push_ratings:
                results["ratings"] = await mdblist_client.push_ratings(
                    settings.mdblist_api_key, ratings_payload
                )
            if settings.mdblist_push_watchlist:
                results["watchlist"] = await mdblist_client.push_watchlist(
                    settings.mdblist_api_key, watchlist_payload
                )

            submitted = sum(result["submitted"] for result in results.values())
            not_found = sum(result["not_found"] for result in results.values())
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    processed_items=submitted,
                    errors=not_found,
                    stats={"submitted": submitted, "not_found": not_found, "targets": results},
                )
            )
            await db.commit()
        except Exception as exc:
            logger.exception("MDBList push job %s failed", job_id)
            await db.rollback()
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.failed,
                    error_message=str(exc),
                )
            )
            await db.commit()


def _require_key(settings: UserSettings | None) -> UserSettings:
    if not settings or not settings.mdblist_api_key:
        raise HTTPException(
            status_code=400,
            detail="Configure a valid MDBList API key in Settings first",
        )
    return settings


@router.post("/sync")
async def sync_mdblist(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = _require_key(result.scalar_one_or_none())
    if not any((settings.mdblist_sync_watched, settings.mdblist_sync_ratings, settings.mdblist_sync_watchlist)):
        raise HTTPException(status_code=400, detail="Enable at least one MDBList pull option")

    job = SyncJob(user_id=current_user.id, source=CollectionSource.mdblist, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(run_mdblist_sync, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "MDBList sync is running in the background"}


@router.post("/push")
async def push_mdblist(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = _require_key(result.scalar_one_or_none())
    if not any((settings.mdblist_push_watched, settings.mdblist_push_ratings, settings.mdblist_push_watchlist)):
        raise HTTPException(status_code=400, detail="Enable at least one MDBList push option")

    job = SyncJob(
        user_id=current_user.id,
        source=CollectionSource.mdblist,
        status=SyncStatus.pending,
        job_type="push",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(run_mdblist_push, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "MDBList push is running in the background"}
