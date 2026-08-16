"""Simkl integration router.

Endpoints:
  POST   /simkl/auth/pin/start   – Start PIN auth flow
  POST   /simkl/auth/pin/poll    – Poll for token completion
  DELETE /simkl/auth/disconnect  – Clear stored token
  POST   /simkl/sync             – Trigger a Simkl import (watched history + ratings + lists)
  POST   /simkl/push             – Push Scrob history/ratings to Simkl
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import simkl as simkl_client
from core.enrichment import enrich_media, is_unmapped_tvdb_episode, create_media_safely
from core.rewatch import record_rewatch_progress
from db import get_db, engine
from dependencies import get_current_user
from models.base import CollectionSource, MediaType
from models.events import WatchEvent
from models.lists import List as ListModel, ListItem
from models.media import Media
from models.ratings import Rating, RatingChanges
from models.show import Show
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings
from models.global_settings import GlobalSettings

logger = logging.getLogger(__name__)

router = APIRouter()

TMDB_CONCURRENCY = 10
SIMKL_WATCHLIST_SLUG = "__simkl_watchlist__"
SIMKL_PUSH_BATCH_SIZE = 50


def _require_simkl_config(settings: UserSettings) -> None:
    if not settings.simkl_client_id:
        raise HTTPException(
            status_code=503,
            detail="Simkl Client ID is not configured. Add it in Settings → Sync → Simkl.",
        )


# ── PIN Authentication ────────────────────────────────────────────────────────

@router.post("/auth/pin/start")
async def simkl_pin_start(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiate PIN authentication. Returns user_code + url."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = UserSettings(user_id=current_user.id)
        db.add(settings)

    _require_simkl_config(settings)

    data = await simkl_client.start_pin_auth(settings.simkl_client_id)

    settings.simkl_device_code = data["user_code"]
    await db.commit()

    return {
        "user_code": data["user_code"],
        "url": data.get("url") or f"https://simkl.com/pin/{data['user_code']}",
        "expires_in": data.get("expires_in", 600),
        "interval": data.get("interval", 5),
    }


@router.post("/auth/pin/poll")
async def simkl_pin_poll(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check if the user has authorised via PIN. Call repeatedly per the interval."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if not settings or not settings.simkl_device_code:
        raise HTTPException(status_code=400, detail="No pending PIN authorization. Call /auth/pin/start first.")

    _require_simkl_config(settings)

    try:
        access_token = await simkl_client.poll_pin_token(
            settings.simkl_client_id,
            settings.simkl_device_code,
        )
    except Exception as exc:
        settings.simkl_device_code = None
        await db.commit()
        raise HTTPException(status_code=400, detail=f"Authorization failed: {exc}")

    if access_token is None:
        return {"status": "pending"}

    settings.simkl_access_token = access_token
    settings.simkl_device_code = None
    await db.commit()

    return {"status": "connected"}


@router.delete("/auth/disconnect")
async def simkl_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear stored Simkl token."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if settings:
        settings.simkl_access_token = None
        settings.simkl_device_code = None
        await db.commit()

    return {"status": "disconnected"}


# ── Sync helpers (shared with run_simkl_sync) ─────────────────────────────────

async def _get_or_create_show(db: AsyncSession, tmdb_id: int, title: str, api_key: str | None) -> Show | None:
    result = await db.execute(select(Show).where(Show.tmdb_id == tmdb_id))
    show = result.scalars().first()
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
    media = result.scalars().first()
    if media:
        return media
    media, _created = await create_media_safely(db, tmdb_id, MediaType.movie, title=title)
    await enrich_media(media, api_key=api_key)
    return media


async def _get_or_create_series_media(db: AsyncSession, tmdb_id: int, title: str, api_key: str | None) -> Media | None:
    result = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.series)
    )
    media = result.scalars().first()
    if media:
        return media
    media, _created = await create_media_safely(db, tmdb_id, MediaType.series, title=title)
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
    media = result.scalars().first()
    if media:
        return media
    from core import tmdb
    try:
        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)
        async with semaphore:
            season_data = await tmdb.get_season(show_tmdb_id, season_number, api_key=api_key)
        ep_map = {ep["episode_number"]: ep for ep in season_data.get("episodes", [])}
        ep = ep_map.get(episode_number)
        if not ep:
            # TMDB has no such episode (provider numbering mismatch) — don't fabricate
            # a placeholder row for it; see routers/trakt.py's twin of this function.
            logger.warning(
                "Simkl episode s%se%s not found on TMDB for show tmdb=%s — skipping",
                season_number, episode_number, show_tmdb_id,
            )
            return None
        media, _created = await create_media_safely(
            db,
            ep["id"],
            MediaType.episode,
            title=ep["name"],
            overview=ep.get("overview"),
            poster_path=tmdb.poster_url(ep.get("still_path"), size="w500"),
            release_date=ep.get("air_date"),
            tmdb_rating=ep.get("vote_average"),
            runtime=ep.get("runtime"),  # see #169
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number,
            tmdb_data={"runtime": ep.get("runtime"), "cast": []},
        )
        return media
    except Exception as exc:
        logger.warning("Could not fetch episode s%se%s for show tmdb=%s: %s", season_number, episode_number, show_tmdb_id, exc)
        return None


def _parse_watched_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        from dateutil import parser as dt_parser
        dt = dt_parser.isoparse(raw)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _simkl_rating_value(item: dict) -> float | None:
    """Extract a Simkl sync/ratings entry's user-set rating, or None if this
    item isn't actually rated.

    Regression guard for issue #112: /sync/ratings returns the same per-item
    shape as /sync/all-items (status, last_watched_at, etc.) for every item
    the user has, rated or not - the rating itself lives under "user_rating"
    (most entries have it as None), not the generic "rating" key that
    Simkl's single-item rate/unrate endpoints use elsewhere in this API.
    Reading the wrong key meant every entry looked unrated and nothing ever
    imported, silently.
    """
    val = item.get("user_rating")
    return float(val) if val else None


# ── Background sync job ───────────────────────────────────────────────────────

async def run_simkl_sync(user_id: int, job_id: int) -> None:
    from routers.sync import SyncCancelled, _raise_if_cancelled
    print(f"Starting Simkl sync for user {user_id}, job {job_id}")
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

            if not settings or not settings.simkl_access_token:
                err = "Simkl is not connected"
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                await db.commit()
                return

            client_id    = settings.simkl_client_id
            access_token = settings.simkl_access_token

            _gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
            _gs = _gs_result.scalar_one_or_none()
            api_key = settings.tmdb_api_key or (_gs.tmdb_api_key if _gs else None)

            stats: dict[str, int] = {"movies": 0, "episodes": 0, "ratings": 0, "lists": 0, "list_items": 0, "skipped": 0, "errors": 0}
            _new_watched: set[int] = set()
            _new_ratings: RatingChanges = {}

            print("  Fetching all items from Simkl…")
            all_items = await simkl_client.get_all_items(client_id, access_token)
            raw_movies = all_items.get("movies", []) or []
            raw_shows  = all_items.get("shows",  []) or []

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(raw_movies) + len(raw_shows)))
            await db.commit()

            # ── Pre-load existing watch events ────────────────────────────────
            we_res = await db.execute(select(WatchEvent.media_id).where(WatchEvent.user_id == user_id))
            existing_watched: set[int] = {row[0] for row in we_res}

            # ── Movies ────────────────────────────────────────────────────────
            if settings.simkl_sync_watched:
                print(f"  Processing {len(raw_movies)} movies from Simkl…")
                for item in raw_movies:
                    if item.get("status") != "completed":
                        continue
                    movie_data = item.get("movie", {})
                    tmdb_id = movie_data.get("ids", {}).get("tmdb")
                    if not tmdb_id:
                        stats["skipped"] += 1
                        continue
                    tmdb_id = int(tmdb_id)
                    try:
                        async with db.begin_nested():
                            media = await _get_or_create_movie_media(db, tmdb_id, movie_data.get("title", ""), api_key)
                            if not media:
                                stats["errors"] += 1
                                continue
                            if media.id not in existing_watched:
                                watched_at = _parse_watched_at(item.get("last_watched_at"))
                                db.add(WatchEvent(
                                    user_id=user_id,
                                    media_id=media.id,
                                    # A dateless Simkl play stays dateless (the
                                    # column is nullable, same as the Trakt
                                    # import) — stamping "now" floods history
                                    # with "watched today" on first sync (#127).
                                    watched_at=watched_at,
                                    completed=True,
                                    play_count=1,
                                ))
                                existing_watched.add(media.id)
                                _new_watched.add(media.id)
                                stats["movies"] += 1
                            else:
                                stats["skipped"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Simkl movie tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1
                await db.commit()
                await _raise_if_cancelled(db, job_id)

            # ── Shows / Episodes ──────────────────────────────────────────────
            if settings.simkl_sync_watched:
                print(f"  Processing {len(raw_shows)} shows from Simkl…")
                for show_entry in raw_shows:
                    show_data  = show_entry.get("show", {})
                    show_tmdb_id = show_data.get("ids", {}).get("tmdb")
                    if not show_tmdb_id:
                        stats["skipped"] += 1
                        continue
                    show_tmdb_id = int(show_tmdb_id)

                    seasons = show_entry.get("seasons") or []
                    if not seasons:
                        # No per-episode data available for this show
                        stats["skipped"] += 1
                        continue

                    try:
                        async with db.begin_nested():
                            show = await _get_or_create_show(db, show_tmdb_id, show_data.get("title", ""), api_key)
                            if not show:
                                stats["errors"] += 1
                                continue
                            await db.flush()

                        for season_entry in seasons:
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
                                            watched_at = _parse_watched_at(ep_entry.get("watched_at"))
                                            event = WatchEvent(
                                                user_id=user_id,
                                                media_id=media.id,
                                                # See the movie branch above (#127).
                                                watched_at=watched_at,
                                                completed=True,
                                                play_count=1,
                                            )
                                            db.add(event)
                                            await db.flush()
                                            await record_rewatch_progress(db, user_id, media.id, event.id)
                                            existing_watched.add(media.id)
                                            _new_watched.add(media.id)
                                            stats["episodes"] += 1
                                        else:
                                            stats["skipped"] += 1
                                except Exception as exc:
                                    logger.warning("Error processing Simkl episode s%se%s show tmdb=%s: %s", season_num, ep_num, show_tmdb_id, exc)
                                    stats["errors"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Simkl show tmdb=%s: %s", show_tmdb_id, exc)
                        stats["errors"] += 1

                await db.commit()
                await _raise_if_cancelled(db, job_id)

            # ── Ratings ───────────────────────────────────────────────────────
            if settings.simkl_sync_ratings:
                print("  Fetching ratings from Simkl…")
                try:
                    ratings_data = await simkl_client.get_ratings(client_id, access_token)
                except Exception as exc:
                    logger.warning("Failed to fetch Simkl ratings: %s", exc)
                    ratings_data = {}

                rat_res = await db.execute(
                    select(Rating.media_id).where(
                        Rating.user_id == user_id,
                        Rating.season_number.is_(None),
                        Rating.episode_order.is_(None),
                    )
                )
                existing_rated: set[int] = {row[0] for row in rat_res}

                for item in ratings_data.get("movies", []):
                    movie_data = item.get("movie", {})
                    tmdb_id = movie_data.get("ids", {}).get("tmdb")
                    rating_val = _simkl_rating_value(item)
                    if not tmdb_id or not rating_val:
                        continue
                    tmdb_id = int(tmdb_id)
                    try:
                        async with db.begin_nested():
                            media = await _get_or_create_movie_media(db, tmdb_id, movie_data.get("title", ""), api_key)
                            if not media:
                                continue
                            if media.id not in existing_rated:
                                db.add(Rating(user_id=user_id, media_id=media.id, rating=float(rating_val)))
                                existing_rated.add(media.id)
                                _new_ratings[(media.id, None)] = float(rating_val)
                                stats["ratings"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Simkl movie rating tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1

                for item in ratings_data.get("shows", []):
                    show_data = item.get("show", {})
                    tmdb_id = show_data.get("ids", {}).get("tmdb")
                    rating_val = _simkl_rating_value(item)
                    if not tmdb_id or not rating_val:
                        continue
                    tmdb_id = int(tmdb_id)
                    try:
                        async with db.begin_nested():
                            media = await _get_or_create_series_media(db, tmdb_id, show_data.get("title", ""), api_key)
                            if media.id not in existing_rated:
                                db.add(Rating(user_id=user_id, media_id=media.id, rating=float(rating_val)))
                                existing_rated.add(media.id)
                                _new_ratings[(media.id, None)] = float(rating_val)
                                stats["ratings"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Simkl show rating tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1

                await db.commit()
                await _raise_if_cancelled(db, job_id)

            # ── Watchlist / plan-to-watch ─────────────────────────────────────
            if settings.simkl_sync_lists:
                print("  Processing Simkl watchlist (plan to watch)…")
                wl_result = await db.execute(
                    select(ListModel).where(
                        ListModel.user_id == user_id,
                        ListModel.trakt_slug == SIMKL_WATCHLIST_SLUG,
                    )
                )
                watchlist = wl_result.scalar_one_or_none()
                if not watchlist:
                    watchlist = ListModel(user_id=user_id, name="Simkl - Watchlist", trakt_slug=SIMKL_WATCHLIST_SLUG)
                    db.add(watchlist)
                    await db.flush()
                    stats["lists"] += 1

                wl_items_res = await db.execute(select(ListItem.media_id).where(ListItem.list_id == watchlist.id))
                wl_existing: set[int] = {row[0] for row in wl_items_res}

                plantowatch_movies = [m for m in raw_movies if m.get("status") == "plantowatch"]
                plantowatch_shows  = [s for s in raw_shows  if s.get("status") == "plantowatch"]

                for item in plantowatch_movies:
                    movie_data = item.get("movie", {})
                    tmdb_id = movie_data.get("ids", {}).get("tmdb")
                    if not tmdb_id:
                        continue
                    tmdb_id = int(tmdb_id)
                    try:
                        async with db.begin_nested():
                            media = await _get_or_create_movie_media(db, tmdb_id, movie_data.get("title", ""), api_key)
                        if media and media.id not in wl_existing:
                            db.add(ListItem(list_id=watchlist.id, media_id=media.id))
                            wl_existing.add(media.id)
                            stats["list_items"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Simkl watchlist movie tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1

                for item in plantowatch_shows:
                    show_data = item.get("show", {})
                    tmdb_id = show_data.get("ids", {}).get("tmdb")
                    if not tmdb_id:
                        continue
                    tmdb_id = int(tmdb_id)
                    try:
                        async with db.begin_nested():
                            media = await _get_or_create_series_media(db, tmdb_id, show_data.get("title", ""), api_key)
                        if media and media.id not in wl_existing:
                            db.add(ListItem(list_id=watchlist.id, media_id=media.id))
                            wl_existing.add(media.id)
                            stats["list_items"] += 1
                    except Exception as exc:
                        logger.warning("Error processing Simkl watchlist show tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1

                await db.commit()

            print(
                f"Simkl sync job {job_id} completed. "
                f"Movies: {stats['movies']} new. "
                f"Episodes: {stats['episodes']} new. "
                f"Ratings: {stats['ratings']} new. "
                f"Lists: {stats['lists']} new, {stats['list_items']} items. "
                f"Skipped: {stats['skipped']}. Errors: {stats['errors']}."
            )
            # A pull only populates scrob's own data — it never automatically pushes
            # to other connections. Users push explicitly per-service (the "Push"
            # buttons), so a bulk pull of thousands of items doesn't unexpectedly
            # blast them out everywhere else at once.
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats=stats,
                    processed_items=stats["movies"] + stats["episodes"] + stats["ratings"],
                )
            )
            await db.commit()

        except SyncCancelled:
            print(f"Simkl sync job {job_id} cancelled")
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.cancelled,
                    stats=stats,
                    processed_items=stats["movies"] + stats["episodes"] + stats["ratings"],
                )
            )
            await db.commit()

        except Exception as exc:
            print(f"Simkl sync job {job_id} failed: {exc}")
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.failed, error_message=str(exc)
                )
            )
            await db.commit()


@router.post("/sync")
async def sync_simkl(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    _require_simkl_config(settings)

    if not settings or not settings.simkl_access_token:
        raise HTTPException(status_code=400, detail="Simkl is not connected")

    _tmdb_key = settings.tmdb_api_key if settings else None
    if not _tmdb_key:
        _gs_r = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        _gs = _gs_r.scalar_one_or_none()
        _tmdb_key = _gs.tmdb_api_key if _gs else None
    if not _tmdb_key:
        raise HTTPException(status_code=400, detail="TMDB API key required for sync")

    job = SyncJob(user_id=current_user.id, source=CollectionSource.simkl, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_simkl_sync, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "Simkl sync is running in the background"}


# ── Push (Scrob → Simkl) ──────────────────────────────────────────────────────

async def _run_simkl_push(user_id: int, job_id: int) -> None:
    from routers.sync import SyncCancelled, _raise_if_cancelled
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running)
            )
            await db.commit()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            if not settings or not settings.simkl_access_token or not settings.simkl_client_id:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message="Simkl is not connected"))
                await db.commit()
                return

            all_media_ids: set[int] = set()
            watched_ids:   set[int] = set()
            ratings_map:   dict[int, float] = {}

            if settings.simkl_push_watched:
                watched_result = await db.execute(
                    select(WatchEvent.media_id).where(WatchEvent.user_id == user_id).distinct()
                )
                watched_ids = {row[0] for row in watched_result.all()}
                all_media_ids |= watched_ids

                from routers.sync import _latest_watched_at
                watched_at_by_media = await _latest_watched_at(db, user_id, list(watched_ids))

            if settings.simkl_push_ratings:
                ratings_result = await db.execute(
                    select(Rating.media_id, Rating.rating).where(
                        Rating.user_id == user_id,
                        Rating.rating.isnot(None),
                        Rating.season_number.is_(None),
                        Rating.episode_order.is_(None),
                    )
                )
                ratings_map = {row[0]: row[1] for row in ratings_result.all()}
                all_media_ids |= set(ratings_map.keys())

            if not all_media_ids:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats={"succeeded": 0, "failed": 0}, processed_items=0, total_items=0))
                await db.commit()
                return

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(all_media_ids)))
            await db.commit()

            media_result = await db.execute(select(Media).where(Media.id.in_(all_media_ids)))
            media_by_id: dict[int, Media] = {m.id: m for m in media_result.scalars().all()}

            show_ids = {m.show_id for m in media_by_id.values() if m.show_id}
            shows_by_id: dict[int, Show] = {}
            if show_ids:
                shows_result = await db.execute(select(Show).where(Show.id.in_(show_ids)))
                shows_by_id = {s.id: s for s in shows_result.scalars().all()}

            movie_candidates: list[tuple[int, datetime]] = []
            episode_candidates: list[tuple[int, int, int, datetime]] = []

            if settings.simkl_push_watched:
                for mid in watched_ids:
                    media = media_by_id.get(mid)
                    if not media or not media.tmdb_id or is_unmapped_tvdb_episode(media):
                        continue
                    # Simkl has no unknown-date representation, and watched_at=None
                    # means "stamp as now" on its side — skip rather than fabricate.
                    watched_at = watched_at_by_media.get(mid)
                    if watched_at is None:
                        continue
                    if media.media_type == MediaType.movie:
                        movie_candidates.append((media.tmdb_id, watched_at))
                    elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                        show = shows_by_id.get(media.show_id)
                        if show and show.tmdb_id:
                            episode_candidates.append((show.tmdb_id, media.season_number, media.episode_number, watched_at))
                # Push oldest-first so a mid-run failure (e.g. a rate limit) drops
                # the least-recently-watched items rather than an arbitrary slice.
                movie_candidates.sort(key=lambda item: item[1])
                episode_candidates.sort(key=lambda item: item[3])

            movie_rating_candidates: list[tuple[int, float]] = []
            show_rating_candidates: list[tuple[int, float]] = []

            if settings.simkl_push_ratings:
                # Simkl has no "rated but not watched" state: rating an item that isn't
                # already in one of its lists auto-files it as watched (today's date),
                # and removing that watched status removes the rating right along with
                # it. So there's no way to represent "rated, never watched" on Simkl —
                # only push ratings for items scrob also considers watched.
                # Determined independent of settings.simkl_push_watched — a movie/show
                # can have local watch history even when the user chose not to push
                # watched history to Simkl this run.
                rating_media_ids = list(ratings_map.keys())
                watch_check_result = await db.execute(
                    select(WatchEvent.media_id).where(
                        WatchEvent.user_id == user_id,
                        WatchEvent.media_id.in_(rating_media_ids),
                    ).distinct()
                )
                media_ids_with_watch_event = {row[0] for row in watch_check_result.all()}

                rated_show_tmdb_ids = {
                    media_by_id[mid].tmdb_id
                    for mid in rating_media_ids
                    if media_by_id.get(mid) and media_by_id[mid].media_type == MediaType.series and media_by_id[mid].tmdb_id
                }
                shows_with_watched_episode: set[int] = set()
                if rated_show_tmdb_ids:
                    watched_show_result = await db.execute(
                        select(Show.tmdb_id)
                        .join(Media, Media.show_id == Show.id)
                        .join(WatchEvent, WatchEvent.media_id == Media.id)
                        .where(WatchEvent.user_id == user_id, Show.tmdb_id.in_(rated_show_tmdb_ids))
                        .distinct()
                    )
                    shows_with_watched_episode = {row[0] for row in watched_show_result.all()}

                for mid, rating in ratings_map.items():
                    media = media_by_id.get(mid)
                    if not media or not media.tmdb_id or is_unmapped_tvdb_episode(media):
                        continue
                    if media.media_type == MediaType.movie:
                        if mid not in media_ids_with_watch_event:
                            continue
                        movie_rating_candidates.append((media.tmdb_id, rating))
                    elif media.media_type == MediaType.series:
                        if media.tmdb_id not in shows_with_watched_episode:
                            continue
                        show_rating_candidates.append((media.tmdb_id, rating))
                    # MediaType.episode ratings have no Simkl equivalent — Simkl only
                    # supports whole-movie/whole-show ratings, not season or episode
                    # ratings — so they're intentionally dropped rather than pushed
                    # as a (wrong) show-level rating.

            # Combine items into multi-item request bodies (Simkl's /sync/history and
            # /ratings endpoints both accept arrays) instead of one HTTP call per item —
            # pushing thousands of items individually blows through Simkl's rate limit
            # (1000 requests/10 min) with no backoff, failing everything past the cliff.
            push_tasks: list[tuple[str, int, "asyncio.Future"]] = []

            history_pending: list[tuple[str, tuple]] = [("movie", item) for item in movie_candidates]
            history_pending.extend(("episode", item) for item in episode_candidates)
            for i in range(0, len(history_pending), SIMKL_PUSH_BATCH_SIZE):
                chunk = history_pending[i:i + SIMKL_PUSH_BATCH_SIZE]
                movies = [item for kind, item in chunk if kind == "movie"]
                episodes = [item for kind, item in chunk if kind == "episode"]
                push_tasks.append((
                    "watched",
                    len(chunk),
                    simkl_client.add_history_batch(settings.simkl_client_id, settings.simkl_access_token, movies, episodes),
                ))

            rating_pending: list[tuple[str, tuple]] = [("movie", item) for item in movie_rating_candidates]
            rating_pending.extend(("show", item) for item in show_rating_candidates)
            for i in range(0, len(rating_pending), SIMKL_PUSH_BATCH_SIZE):
                chunk = rating_pending[i:i + SIMKL_PUSH_BATCH_SIZE]
                movie_ratings = [item for kind, item in chunk if kind == "movie"]
                show_ratings = [item for kind, item in chunk if kind == "show"]
                push_tasks.append((
                    "ratings",
                    len(chunk),
                    simkl_client.set_ratings_batch(settings.simkl_client_id, settings.simkl_access_token, movie_ratings, show_ratings),
                ))

            total = sum(item_count for _, item_count, _ in push_tasks)

            if not push_tasks:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats={"succeeded": 0, "failed": 0}, processed_items=0))
                await db.commit()
                return

            print(f"Simkl full push: pushing {total} items in {len(push_tasks)} batch requests…")
            REQUEST_CONCURRENCY = 50
            succeeded = 0
            failed = 0
            for i in range(0, len(push_tasks), REQUEST_CONCURRENCY):
                batch = push_tasks[i:i + REQUEST_CONCURRENCY]
                results = await asyncio.gather(*[task for _, _, task in batch], return_exceptions=True)
                for (category, item_count, _), result in zip(batch, results):
                    if isinstance(result, Exception):
                        failed += item_count
                        logger.warning("Simkl push batch failed (%s, %d items): %s", category, item_count, result)
                    else:
                        succeeded += item_count
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=succeeded + failed))
                await db.commit()
                await _raise_if_cancelled(db, job_id)
            print(f"Simkl full push: {succeeded}/{total} succeeded")

            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats={"succeeded": succeeded, "failed": failed},
                    processed_items=succeeded + failed,
                )
            )
            await db.commit()

        except SyncCancelled:
            print(f"Simkl push job {job_id} cancelled")
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                status=SyncStatus.cancelled,
                stats={"succeeded": succeeded, "failed": failed},
                processed_items=succeeded + failed,
            ))
            await db.commit()

        except Exception as exc:
            print(f"Simkl push job {job_id} failed: {exc}")
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=str(exc)))
            await db.commit()


@router.post("/push")
async def push_simkl(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    _require_simkl_config(settings)
    if not settings or not settings.simkl_access_token:
        raise HTTPException(status_code=400, detail="Simkl is not connected")
    if not settings.simkl_push_watched and not settings.simkl_push_ratings:
        raise HTTPException(status_code=400, detail="Enable 'Scrob → Simkl' push flags first")
    job = SyncJob(user_id=current_user.id, source=CollectionSource.simkl, status=SyncStatus.pending, job_type="push")
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(_run_simkl_push, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "Simkl push is running in the background"}
