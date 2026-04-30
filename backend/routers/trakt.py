"""Trakt.tv integration router.

Endpoints:
  POST /trakt/auth/device/start   – Start device auth flow
  POST /trakt/auth/device/poll    – Poll for token completion
  DELETE /trakt/auth/disconnect   – Revoke token and clear stored credentials
  POST /trakt/sync                – Trigger a Trakt import (watched history + ratings)
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import trakt as trakt_client
from core.enrichment import enrich_media
from db import get_db, engine
from dependencies import get_current_user
from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.ratings import Rating
from models.show import Show
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings

logger = logging.getLogger(__name__)

router = APIRouter()

TMDB_CONCURRENCY = 10


def _require_trakt_config(settings: UserSettings):
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise HTTPException(
            status_code=503,
            detail="Trakt Client ID and Client Secret are not configured. Add them in Settings → Sync → Trakt.",
        )


# ── Device Authentication ─────────────────────────────────────────────────────

@router.post("/auth/device/start")
async def trakt_device_start(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiate device authentication. Returns user_code + verification_url."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = UserSettings(user_id=current_user.id)
        db.add(settings)

    _require_trakt_config(settings)

    data = await trakt_client.start_device_auth(settings.trakt_client_id)

    settings.trakt_device_code = data["device_code"]
    await db.commit()

    return {
        "user_code": data["user_code"],
        "verification_url": data["verification_url"],
        "expires_in": data["expires_in"],
        "interval": data["interval"],
    }


@router.post("/auth/device/poll")
async def trakt_device_poll(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check if the user has authorized the device. Call repeatedly per the interval."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if not settings or not settings.trakt_device_code:
        raise HTTPException(status_code=400, detail="No pending device authorization. Call /auth/device/start first.")

    _require_trakt_config(settings)

    try:
        token_data = await trakt_client.poll_device_token(
            settings.trakt_client_id,
            settings.trakt_client_secret,
            settings.trakt_device_code,
        )
    except Exception as exc:
        # Permanent failure (expired / denied)
        settings.trakt_device_code = None
        await db.commit()
        raise HTTPException(status_code=400, detail=f"Authorization failed: {exc}")

    if token_data is None:
        # Still pending — tell the frontend to keep polling
        return {"status": "pending"}

    # Success — store the tokens
    settings.trakt_access_token = token_data["access_token"]
    settings.trakt_refresh_token = token_data["refresh_token"]
    settings.trakt_token_expires_at = token_data.get("expires_in", 0) + int(datetime.now(timezone.utc).timestamp())
    settings.trakt_device_code = None
    await db.commit()

    return {"status": "connected"}


@router.delete("/auth/disconnect")
async def trakt_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revoke the Trakt token and clear stored credentials."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if settings and settings.trakt_access_token:
        if settings.trakt_client_id and settings.trakt_client_secret:
            await trakt_client.revoke_token(
                settings.trakt_client_id,
                settings.trakt_client_secret,
                settings.trakt_access_token,
            )
        settings.trakt_access_token = None
        settings.trakt_refresh_token = None
        settings.trakt_token_expires_at = None
        settings.trakt_device_code = None
        await db.commit()

    return {"status": "disconnected"}


# ── Sync ─────────────────────────────────────────────────────────────────────

async def _get_or_create_show(db: AsyncSession, tmdb_id: int, title: str, api_key: str | None) -> Show | None:
    result = await db.execute(select(Show).where(Show.tmdb_id == tmdb_id))
    show = result.scalar_one_or_none()
    if show:
        return show
    from core import tmdb
    try:
        d = await tmdb.get_show(tmdb_id, api_key=api_key)
        show = Show(
            tmdb_id=tmdb_id,
            title=d.get("name") or title,
            original_title=d.get("original_name"),
            overview=d.get("overview"),
            poster_path=tmdb.poster_url(d.get("poster_path")),
            backdrop_path=tmdb.poster_url(d.get("backdrop_path"), size="w1280"),
            tmdb_rating=d.get("vote_average"),
            status=d.get("status"),
            tagline=d.get("tagline"),
            first_air_date=d.get("first_air_date"),
            last_air_date=d.get("last_air_date"),
            tmdb_data={
                "genres": [g["name"] for g in d.get("genres", [])],
                "external_ids": d.get("external_ids", {}),
                "original_language": d.get("original_language"),
                "seasons": [
                    {
                        "season_number": s["season_number"],
                        "poster_path": tmdb.poster_url(s.get("poster_path")),
                        "episode_count": s["episode_count"],
                        "name": s["name"],
                    }
                    for s in d.get("seasons", [])
                ],
            },
        )
        db.add(show)
        await db.flush()
        return show
    except Exception as exc:
        logger.warning("Could not fetch show tmdb=%s: %s", tmdb_id, exc)
        return None


async def _get_or_create_movie_media(db: AsyncSession, tmdb_id: int, title: str, api_key: str | None) -> Media | None:
    result = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.movie)
    )
    media = result.scalar_one_or_none()
    if media:
        return media
    media = Media(tmdb_id=tmdb_id, media_type=MediaType.movie, title=title)
    db.add(media)
    await db.flush()
    await enrich_media(media, api_key=api_key)
    return media


async def _get_or_create_episode_media(
    db: AsyncSession,
    show_id: int,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
    api_key: str | None,
) -> Media | None:
    result = await db.execute(
        select(Media).where(
            Media.show_id == show_id,
            Media.season_number == season_number,
            Media.episode_number == episode_number,
            Media.media_type == MediaType.episode,
        )
    )
    media = result.scalar_one_or_none()
    if media:
        return media
    from core import tmdb
    # Fetch episode detail from TMDB
    try:
        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)
        async with semaphore:
            season_data = await tmdb.get_season(show_tmdb_id, season_number, api_key=api_key)
        ep_map = {ep["episode_number"]: ep for ep in season_data.get("episodes", [])}
        ep = ep_map.get(episode_number)
        media = Media(
            tmdb_id=ep["id"] if ep else None,
            media_type=MediaType.episode,
            title=ep["name"] if ep else f"S{season_number:02d}E{episode_number:02d}",
            overview=ep.get("overview") if ep else None,
            poster_path=tmdb.poster_url(ep.get("still_path"), size="w500") if ep else None,
            release_date=ep.get("air_date") if ep else None,
            tmdb_rating=ep.get("vote_average") if ep else None,
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number,
            tmdb_data={"runtime": ep.get("runtime"), "cast": []} if ep else {},
        )
        db.add(media)
        await db.flush()
        return media
    except Exception as exc:
        logger.warning("Could not fetch episode s%se%s for show tmdb=%s: %s", season_number, episode_number, show_tmdb_id, exc)
        return None


async def run_trakt_sync(user_id: int, job_id: int):
    logger.info("Starting Trakt sync for user %s, job %s", user_id, job_id)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.running, processed_items=0, total_items=0
                )
            )
            await db.commit()

            result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = result.scalar_one_or_none()

            if not settings or not settings.trakt_access_token:
                err = "Trakt is not connected"
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                await db.commit()
                return

            if not settings.trakt_client_id:
                err = "Trakt Client ID not configured. Add it in Settings → Sync → Trakt."
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                await db.commit()
                return

            # Refresh the access token if it has expired
            if not await trakt_client.validate_token(settings.trakt_client_id, settings.trakt_access_token):
                if settings.trakt_refresh_token and settings.trakt_client_secret:
                    try:
                        from datetime import datetime, timezone
                        token_data = await trakt_client.refresh_access_token(
                            settings.trakt_client_id,
                            settings.trakt_client_secret,
                            settings.trakt_refresh_token,
                        )
                        settings.trakt_access_token = token_data["access_token"]
                        settings.trakt_refresh_token = token_data["refresh_token"]
                        settings.trakt_token_expires_at = token_data.get("expires_in", 0) + int(datetime.now(timezone.utc).timestamp())
                        await db.commit()
                    except Exception as exc:
                        err = f"Trakt token expired and refresh failed: {exc}"
                        await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                        await db.commit()
                        return
                else:
                    err = "Trakt token expired. Please reconnect Trakt in Settings."
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                    await db.commit()
                    return

            client_id = settings.trakt_client_id
            access_token = settings.trakt_access_token
            api_key = settings.tmdb_api_key
            sync_watched = settings.trakt_sync_watched
            sync_ratings = settings.trakt_sync_ratings

            stats = {"movies": 0, "episodes": 0, "ratings": 0, "skipped": 0, "errors": 0}
            _new_watched: set[int] = set()
            _new_ratings: dict[int, float] = {}

            # ── Watched Movies ────────────────────────────────────────────────
            if sync_watched:
                logger.info("  Fetching watched movies from Trakt...")
                watched_movies = await trakt_client.get_watched_movies(client_id, access_token)
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(watched_movies)))
                await db.commit()

                # Pre-load existing watch events for this user
                we_res = await db.execute(
                    select(WatchEvent.media_id).where(WatchEvent.user_id == user_id)
                )
                existing_watched: set[int] = {row[0] for row in we_res}

                for item in watched_movies:
                    movie_data = item.get("movie", {})
                    tmdb_id = movie_data.get("ids", {}).get("tmdb")
                    if not tmdb_id:
                        stats["skipped"] += 1
                        continue
                    try:
                        async with db.begin_nested():
                            media = await _get_or_create_movie_media(db, tmdb_id, movie_data.get("title", ""), api_key)
                            if not media:
                                stats["errors"] += 1
                                continue
                            if media.id not in existing_watched:
                                last_watched = item.get("last_watched_at")
                                watched_at = None
                                if last_watched:
                                    from dateutil import parser as dt_parser
                                    dt = dt_parser.isoparse(last_watched)
                                    if dt.tzinfo:
                                        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                                    watched_at = dt
                                db.add(WatchEvent(
                                    user_id=user_id,
                                    media_id=media.id,
                                    watched_at=watched_at or datetime.utcnow(),
                                    completed=True,
                                    play_count=item.get("plays", 1),
                                ))
                                existing_watched.add(media.id)
                                _new_watched.add(media.id)
                                stats["movies"] += 1
                            else:
                                stats["skipped"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Trakt movie tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1

                await db.commit()

            # ── Watched Shows / Episodes ──────────────────────────────────────
            if sync_watched:
                logger.info("  Fetching watched shows from Trakt...")
                watched_shows = await trakt_client.get_watched_shows(client_id, access_token)

                # Re-fetch watched set (may have grown from movie sync)
                we_res = await db.execute(
                    select(WatchEvent.media_id).where(WatchEvent.user_id == user_id)
                )
                existing_watched = {row[0] for row in we_res}

                semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

                async def process_show(show_entry: dict):
                    show_data = show_entry.get("show", {})
                    show_tmdb_id = show_data.get("ids", {}).get("tmdb")
                    if not show_tmdb_id:
                        stats["skipped"] += 1
                        return

                    async with semaphore:
                        try:
                            async with db.begin_nested():
                                show = await _get_or_create_show(db, show_tmdb_id, show_data.get("title", ""), api_key)
                                if not show:
                                    stats["errors"] += 1
                                    return
                                await db.flush()

                            for season_entry in show_entry.get("seasons", []):
                                season_num = season_entry.get("number")
                                if season_num is None or season_num == 0:
                                    continue
                                for ep_entry in season_entry.get("episodes", []):
                                    ep_num = ep_entry.get("number")
                                    if ep_num is None:
                                        continue
                                    try:
                                        async with db.begin_nested():
                                            media = await _get_or_create_episode_media(
                                                db, show.id, show_tmdb_id, season_num, ep_num, api_key
                                            )
                                            if not media:
                                                stats["errors"] += 1
                                                continue
                                            if media.id not in existing_watched:
                                                last_watched = ep_entry.get("last_watched_at")
                                                watched_at = None
                                                if last_watched:
                                                    from dateutil import parser as dt_parser
                                                    dt = dt_parser.isoparse(last_watched)
                                                    if dt.tzinfo:
                                                        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                                                    watched_at = dt
                                                db.add(WatchEvent(
                                                    user_id=user_id,
                                                    media_id=media.id,
                                                    watched_at=watched_at or datetime.utcnow(),
                                                    completed=True,
                                                    play_count=ep_entry.get("plays", 1),
                                                ))
                                                existing_watched.add(media.id)
                                                _new_watched.add(media.id)
                                                stats["episodes"] += 1
                                            else:
                                                stats["skipped"] += 1
                                    except Exception as exc:
                                        logger.warning("Error processing episode s%se%s for show tmdb=%s: %s", season_num, ep_num, show_tmdb_id, exc)
                                        stats["errors"] += 1
                        except Exception as exc:
                            logger.warning("Error processing Trakt show tmdb=%s: %s", show_tmdb_id, exc)
                            stats["errors"] += 1

                await asyncio.gather(*[process_show(s) for s in watched_shows])
                await db.commit()

            # ── Ratings ───────────────────────────────────────────────────────
            if sync_ratings:
                logger.info("  Fetching ratings from Trakt...")
                ratings_data = await trakt_client.get_ratings(client_id, access_token)

                # Pre-load existing ratings
                rat_res = await db.execute(
                    select(Rating.media_id).where(Rating.user_id == user_id)
                )
                existing_rated: set[int] = {row[0] for row in rat_res}

                # Movies
                for item in ratings_data.get("movies", []):
                    movie_data = item.get("movie", {})
                    tmdb_id = movie_data.get("ids", {}).get("tmdb")
                    if not tmdb_id:
                        continue
                    try:
                        async with db.begin_nested():
                            media = await _get_or_create_movie_media(db, tmdb_id, movie_data.get("title", ""), api_key)
                            if not media:
                                continue
                            if media.id not in existing_rated:
                                db.add(Rating(
                                    user_id=user_id,
                                    media_id=media.id,
                                    rating=float(item.get("rating", 0)),
                                ))
                                existing_rated.add(media.id)
                                _new_ratings[media.id] = float(item.get("rating", 0))
                                stats["ratings"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Trakt movie rating tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1

                # Shows
                for item in ratings_data.get("shows", []):
                    show_data = item.get("show", {})
                    tmdb_id = show_data.get("ids", {}).get("tmdb")
                    if not tmdb_id:
                        continue
                    try:
                        async with db.begin_nested():
                            result = await db.execute(
                                select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.series)
                            )
                            media = result.scalar_one_or_none()
                            if not media:
                                from core import tmdb
                                d = await tmdb.get_show(tmdb_id, api_key=api_key)
                                media = Media(tmdb_id=tmdb_id, media_type=MediaType.series, title=d.get("name") or show_data.get("title", ""))
                                db.add(media)
                                await db.flush()
                                await enrich_media(media, api_key=api_key)
                            if media.id not in existing_rated:
                                db.add(Rating(
                                    user_id=user_id,
                                    media_id=media.id,
                                    rating=float(item.get("rating", 0)),
                                ))
                                existing_rated.add(media.id)
                                _new_ratings[media.id] = float(item.get("rating", 0))
                                stats["ratings"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Trakt show rating tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1

                await db.commit()

            logger.info("Trakt sync job %s completed. Stats: %s", job_id, stats)
            from routers.sync import _fan_out_changes_to_other_connections
            await _fan_out_changes_to_other_connections(db, user_id, None, _new_watched, _new_ratings, settings=settings)
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats=stats,
                    processed_items=stats["movies"] + stats["episodes"] + stats["ratings"],
                )
            )
            await db.commit()

        except Exception as exc:
            logger.error("Trakt sync job %s failed: %s", job_id, exc, exc_info=True)
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.failed, error_message=str(exc)
                )
            )
            await db.commit()


@router.post("/sync")
async def sync_trakt(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    _require_trakt_config(settings)

    if not settings or not settings.trakt_access_token:
        raise HTTPException(status_code=400, detail="Trakt is not connected")
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="TMDB API key required for sync")

    from models.base import CollectionSource
    job = SyncJob(user_id=current_user.id, source=CollectionSource.trakt, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_trakt_sync, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "Trakt sync is running in the background"}
