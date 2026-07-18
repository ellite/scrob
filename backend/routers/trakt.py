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
    media = Media(tmdb_id=tmdb_id, media_type=MediaType.movie, title=title)
    db.add(media)
    await db.flush()
    await enrich_media(media, api_key=api_key)
    return media


async def _get_or_create_series_media(
    db: AsyncSession,
    tmdb_id: int,
    title: str,
    api_key: str | None,
) -> Media | None:
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


def _trakt_rated_at(value: str | None) -> datetime:
    if not value:
        return datetime.utcnow()
    from dateutil import parser as dt_parser

    parsed = dt_parser.isoparse(value)
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _apply_imported_rating(
    db: AsyncSession,
    user_id: int,
    media: Media,
    season_number: int | None,
    item: dict,
    existing: dict[tuple[int, int | None], Rating],
    changed: RatingChanges,
) -> bool:
    rating_value = float(item["rating"])
    rated_at = _trakt_rated_at(item.get("rated_at"))
    key = (media.id, season_number)
    current = existing.get(key)
    if current and current.rating == rating_value:
        current.rated_at = rated_at
        return False
    if current:
        current.rating = rating_value
        current.rated_at = rated_at
    else:
        current = Rating(
            user_id=user_id,
            media_id=media.id,
            season_number=season_number,
            rating=rating_value,
            rated_at=rated_at,
        )
        db.add(current)
        existing[key] = current
    changed[key] = rating_value
    return True


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
    print(f"Starting Trakt sync for user {user_id}, job {job_id}")
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
            _gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
            _gs = _gs_result.scalar_one_or_none()
            api_key = settings.tmdb_api_key or (_gs.tmdb_api_key if _gs else None)
            sync_watched = settings.trakt_sync_watched
            sync_ratings = settings.trakt_sync_ratings

            stats = {"movies": 0, "episodes": 0, "ratings": 0, "lists": 0, "list_items": 0, "skipped": 0, "errors": 0}
            _new_watched: set[int] = set()
            _new_ratings: RatingChanges = {}
            watched_processed = 0

            # ── Watched Movies ────────────────────────────────────────────────
            if sync_watched:
                print(f"  Fetching watched movies from Trakt...")
                watched_movies = await trakt_client.get_watched_movies(client_id, access_token)
                print(f"  {len(watched_movies)} watched movies fetched from Trakt")
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(watched_movies)))
                await db.commit()

                # Pre-load existing watch events for this user
                we_res = await db.execute(
                    select(WatchEvent.media_id).where(WatchEvent.user_id == user_id)
                )
                existing_watched: set[int] = {row[0] for row in we_res}

                for movie_index, item in enumerate(watched_movies, start=1):
                    movie_data = item.get("movie", {})
                    tmdb_id = movie_data.get("ids", {}).get("tmdb")
                    try:
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
                    finally:
                        watched_processed = movie_index
                        if movie_index % 25 == 0 or movie_index == len(watched_movies):
                            await db.execute(
                                update(SyncJob)
                                .where(SyncJob.id == job_id)
                                .values(processed_items=watched_processed)
                            )
                            await db.commit()

                await db.commit()

            # ── Watched Shows / Episodes ──────────────────────────────────────
            if sync_watched:
                print(f"  Fetching watched shows from Trakt...")
                watched_shows = await trakt_client.get_watched_shows(client_id, access_token)
                print(f"  {len(watched_shows)} watched shows fetched from Trakt")

                total_episodes = sum(
                    sum(len(season.get("episodes", [])) for season in s.get("seasons", []) if season.get("number") not in (None, 0))
                    for s in watched_shows
                )
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                    total_items=len(watched_movies) + total_episodes,
                    processed_items=watched_processed,
                ))
                await db.commit()

                # Re-fetch watched set (may have grown from movie sync)
                we_res = await db.execute(
                    select(WatchEvent.media_id).where(WatchEvent.user_id == user_id)
                )
                existing_watched = {row[0] for row in we_res}

                async def process_show(show_entry: dict):
                    show_data = show_entry.get("show", {})
                    show_tmdb_id = show_data.get("ids", {}).get("tmdb")
                    if not show_tmdb_id:
                        stats["skipped"] += 1
                        return

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

                for i, s in enumerate(watched_shows):
                    await process_show(s)
                    watched_processed += sum(
                        len(season.get("episodes", []))
                        for season in s.get("seasons", [])
                        if season.get("number") not in (None, 0)
                    )
                    if (i + 1) % 10 == 0 or i + 1 == len(watched_shows):
                        await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                            processed_items=watched_processed
                        ))
                        await db.commit()
                await db.commit()

            # ── Ratings ───────────────────────────────────────────────────────
            if sync_ratings:
                print("  Fetching ratings from Trakt...")
                ratings_data = await trakt_client.get_ratings(client_id, access_token)

                ratings_result = await db.execute(
                    select(Rating).where(Rating.user_id == user_id)
                )
                existing_ratings = {
                    (rating.media_id, rating.season_number): rating
                    for rating in ratings_result.scalars().all()
                }

                for kind in ("movies", "shows", "seasons"):
                    for item in ratings_data.get(kind, []):
                        try:
                            async with db.begin_nested():
                                season_number: int | None = None
                                if kind == "movies":
                                    movie_data = item.get("movie", {})
                                    tmdb_id = movie_data.get("ids", {}).get("tmdb")
                                    media = (
                                        await _get_or_create_movie_media(
                                            db,
                                            tmdb_id,
                                            movie_data.get("title", ""),
                                            api_key,
                                        )
                                        if tmdb_id
                                        else None
                                    )
                                else:
                                    show_data = item.get("show", {})
                                    tmdb_id = show_data.get("ids", {}).get("tmdb")
                                    if kind == "seasons":
                                        season_number = item.get("season", {}).get("number")
                                    media = (
                                        await _get_or_create_series_media(
                                            db,
                                            tmdb_id,
                                            show_data.get("title", ""),
                                            api_key,
                                        )
                                        if tmdb_id and (kind != "seasons" or season_number is not None)
                                        else None
                                    )

                                if not media:
                                    stats["skipped"] += 1
                                    continue
                                if _apply_imported_rating(
                                    db,
                                    user_id,
                                    media,
                                    season_number,
                                    item,
                                    existing_ratings,
                                    _new_ratings,
                                ):
                                    stats["ratings"] += 1
                                else:
                                    stats["skipped"] += 1
                        except (KeyError, TypeError, ValueError) as exc:
                            logger.warning("Invalid Trakt %s rating: %s", kind, exc)
                            stats["errors"] += 1
                        except Exception as exc:
                            logger.warning("Error processing Trakt %s rating: %s", kind, exc)
                            stats["errors"] += 1

                await db.commit()

            # ── Lists (watchlist + personal lists) ───────────────────────────
            if settings.trakt_sync_lists:
                WATCHLIST_SLUG         = "__watchlist__"
                WATCHLIST_MOVIES_SLUG  = "__watchlist_movies__"
                WATCHLIST_SHOWS_SLUG   = "__watchlist_shows__"
                split_watchlist = getattr(settings, "trakt_watchlist_split", False)

                print(f"  Fetching watchlist from Trakt...")
                watchlist_items = await trakt_client.get_watchlist(client_id, access_token)
                print(f"  {len(watchlist_items)} watchlist items fetched from Trakt")

                if split_watchlist:
                    # ── Split mode: two lists keyed by media type ─────────────
                    async def _get_or_create_split_list(slug: str, name: str) -> ListModel:
                        r = await db.execute(
                            select(ListModel).where(ListModel.user_id == user_id, ListModel.trakt_slug == slug)
                        )
                        lst = r.scalar_one_or_none()
                        if not lst:
                            lst = ListModel(user_id=user_id, name=name, trakt_slug=slug)
                            db.add(lst)
                            await db.flush()
                            stats["lists"] += 1
                        return lst

                    movies_list = await _get_or_create_split_list(WATCHLIST_MOVIES_SLUG, "Trakt - Watchlist (Movies)")
                    shows_list  = await _get_or_create_split_list(WATCHLIST_SHOWS_SLUG,  "Trakt - Watchlist (Shows)")

                    movies_existing = {row[0] for row in (await db.execute(
                        select(ListItem.media_id).where(ListItem.list_id == movies_list.id)
                    )).all()}
                    shows_existing  = {row[0] for row in (await db.execute(
                        select(ListItem.media_id).where(ListItem.list_id == shows_list.id)
                    )).all()}

                    # Reconcile: remove items no longer on Trakt watchlist
                    trakt_movie_tmdb_ids = {
                        e.get("movie", {}).get("ids", {}).get("tmdb")
                        for e in watchlist_items if e.get("type") == "movie"
                    } - {None}
                    trakt_show_tmdb_ids = {
                        e.get("show", {}).get("ids", {}).get("tmdb")
                        for e in watchlist_items if e.get("type") == "show"
                    } - {None}

                    # Remove stale movies
                    if movies_existing:
                        stale_movies_result = await db.execute(
                            select(Media).where(
                                Media.id.in_(movies_existing),
                                Media.tmdb_id.notin_(trakt_movie_tmdb_ids),
                            )
                        )
                        for stale in stale_movies_result.scalars():
                            await db.execute(
                                ListItem.__table__.delete().where(
                                    ListItem.list_id == movies_list.id,
                                    ListItem.media_id == stale.id,
                                )
                            )
                            movies_existing.discard(stale.id)

                    # Remove stale shows
                    if shows_existing:
                        stale_shows_result = await db.execute(
                            select(Media).where(
                                Media.id.in_(shows_existing),
                                Media.tmdb_id.notin_(trakt_show_tmdb_ids),
                            )
                        )
                        for stale in stale_shows_result.scalars():
                            await db.execute(
                                ListItem.__table__.delete().where(
                                    ListItem.list_id == shows_list.id,
                                    ListItem.media_id == stale.id,
                                )
                            )
                            shows_existing.discard(stale.id)

                    for entry in watchlist_items:
                        item_type = entry.get("type")
                        media: Media | None = None
                        try:
                            if item_type == "movie":
                                movie_data = entry.get("movie", {})
                                tmdb_id_item = movie_data.get("ids", {}).get("tmdb")
                                if not tmdb_id_item:
                                    continue
                                async with db.begin_nested():
                                    media = await _get_or_create_movie_media(db, tmdb_id_item, movie_data.get("title", ""), api_key)
                                if media and media.id not in movies_existing:
                                    db.add(ListItem(list_id=movies_list.id, media_id=media.id))
                                    movies_existing.add(media.id)
                                    stats["list_items"] += 1
                            elif item_type == "show":
                                show_data = entry.get("show", {})
                                tmdb_id_item = show_data.get("ids", {}).get("tmdb")
                                if not tmdb_id_item:
                                    continue
                                async with db.begin_nested():
                                    r2 = await db.execute(
                                        select(Media).where(Media.tmdb_id == tmdb_id_item, Media.media_type == MediaType.series)
                                    )
                                    media = r2.scalar_one_or_none()
                                    if not media:
                                        from core import tmdb
                                        d = await tmdb.get_show(tmdb_id_item, api_key=api_key)
                                        media = Media(
                                            tmdb_id=tmdb_id_item,
                                            media_type=MediaType.series,
                                            title=d.get("name") or show_data.get("title", ""),
                                            poster_path=tmdb.poster_url(d.get("poster_path")),
                                            backdrop_path=tmdb.poster_url(d.get("backdrop_path"), size="w1280"),
                                            release_date=d.get("first_air_date"),
                                            tmdb_rating=d.get("vote_average"),
                                            overview=d.get("overview"),
                                            adult=d.get("adult", False),
                                        )
                                        db.add(media)
                                        await db.flush()
                                if media and media.id not in shows_existing:
                                    db.add(ListItem(list_id=shows_list.id, media_id=media.id))
                                    shows_existing.add(media.id)
                                    stats["list_items"] += 1
                        except Exception as exc:
                            logger.warning("Error processing Trakt watchlist item (%s): %s", item_type, exc)
                            stats["errors"] += 1

                else:
                    # ── Unified mode: one list for movies + shows ─────────────
                    wl_result = await db.execute(
                        select(ListModel).where(
                            ListModel.user_id == user_id,
                            ListModel.trakt_slug == WATCHLIST_SLUG,
                        )
                    )
                    watchlist = wl_result.scalar_one_or_none()
                    if not watchlist:
                        watchlist = ListModel(user_id=user_id, name="Trakt - Watchlist", trakt_slug=WATCHLIST_SLUG)
                        db.add(watchlist)
                        await db.flush()
                        stats["lists"] += 1

                    wl_items_result = await db.execute(
                        select(ListItem.media_id).where(ListItem.list_id == watchlist.id)
                    )
                    wl_existing_ids: set[int] = {row[0] for row in wl_items_result}

                    for entry in watchlist_items:
                        item_type = entry.get("type")
                        media: Media | None = None
                        try:
                            if item_type == "movie":
                                movie_data = entry.get("movie", {})
                                tmdb_id_item = movie_data.get("ids", {}).get("tmdb")
                                if not tmdb_id_item:
                                    continue
                                async with db.begin_nested():
                                    media = await _get_or_create_movie_media(db, tmdb_id_item, movie_data.get("title", ""), api_key)
                            elif item_type == "show":
                                show_data = entry.get("show", {})
                                tmdb_id_item = show_data.get("ids", {}).get("tmdb")
                                if not tmdb_id_item:
                                    continue
                                async with db.begin_nested():
                                    r2 = await db.execute(
                                        select(Media).where(Media.tmdb_id == tmdb_id_item, Media.media_type == MediaType.series)
                                    )
                                    media = r2.scalar_one_or_none()
                                    if not media:
                                        from core import tmdb
                                        d = await tmdb.get_show(tmdb_id_item, api_key=api_key)
                                        media = Media(
                                            tmdb_id=tmdb_id_item,
                                            media_type=MediaType.series,
                                            title=d.get("name") or show_data.get("title", ""),
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
                                stats["list_items"] += 1
                        except Exception as exc:
                            logger.warning("Error processing Trakt watchlist item (%s): %s", item_type, exc)
                            stats["errors"] += 1

                await db.commit()

                print(f"  Fetching lists from Trakt...")
                trakt_lists = await trakt_client.get_user_lists(client_id, access_token)
                print(f"  {len(trakt_lists)} lists fetched from Trakt")

                for trakt_list in trakt_lists:
                    list_name = trakt_list.get("name", "")
                    list_slug = trakt_list.get("ids", {}).get("slug") or trakt_list.get("slug")
                    if not list_slug or not list_name:
                        continue

                    local_name = f"Trakt - {list_name}"

                    # Find or create the local list — keyed by trakt_slug, not name
                    existing_list_result = await db.execute(
                        select(ListModel).where(
                            ListModel.user_id == user_id,
                            ListModel.trakt_slug == list_slug,
                        )
                    )
                    local_list = existing_list_result.scalar_one_or_none()
                    if not local_list:
                        local_list = ListModel(
                            user_id=user_id,
                            name=local_name,
                            description=trakt_list.get("description"),
                            trakt_slug=list_slug,
                        )
                        db.add(local_list)
                        await db.flush()
                        stats["lists"] += 1

                    # Pre-load existing list item media_ids to avoid duplicates
                    existing_items_result = await db.execute(
                        select(ListItem.media_id).where(ListItem.list_id == local_list.id)
                    )
                    existing_item_media_ids: set[int] = {row[0] for row in existing_items_result}

                    try:
                        items = await trakt_client.get_list_items(client_id, access_token, list_slug)
                    except Exception as exc:
                        logger.warning("Could not fetch items for Trakt list %s: %s", list_slug, exc)
                        continue

                    for entry in items:
                        item_type = entry.get("type")
                        media: Media | None = None
                        try:
                            if item_type == "movie":
                                movie_data = entry.get("movie", {})
                                tmdb_id = movie_data.get("ids", {}).get("tmdb")
                                if not tmdb_id:
                                    continue
                                async with db.begin_nested():
                                    media = await _get_or_create_movie_media(db, tmdb_id, movie_data.get("title", ""), api_key)
                            elif item_type == "show":
                                show_data = entry.get("show", {})
                                tmdb_id = show_data.get("ids", {}).get("tmdb")
                                if not tmdb_id:
                                    continue
                                async with db.begin_nested():
                                    result2 = await db.execute(
                                        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.series)
                                    )
                                    media = result2.scalar_one_or_none()
                                    if not media:
                                        from core import tmdb
                                        d = await tmdb.get_show(tmdb_id, api_key=api_key)
                                        media = Media(
                                            tmdb_id=tmdb_id,
                                            media_type=MediaType.series,
                                            title=d.get("name") or show_data.get("title", ""),
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

                            if media and media.id not in existing_item_media_ids:
                                db.add(ListItem(list_id=local_list.id, media_id=media.id))
                                existing_item_media_ids.add(media.id)
                                stats["list_items"] += 1
                        except Exception as exc:
                            logger.warning("Error processing Trakt list item (%s): %s", item_type, exc)
                            stats["errors"] += 1

                    await db.commit()

            print(
                f"Trakt sync job {job_id} completed. "
                f"Movies: {stats['movies']} new, {stats.get('skipped', 0)} skipped. "
                f"Episodes: {stats['episodes']} new. "
                f"Ratings: {stats['ratings']} new. "
                f"Lists: {stats['lists']} new, {stats['list_items']} items added. "
                f"Errors: {stats['errors']}."
            )
            from routers.sync import _fan_out_changes_to_other_connections
            await _fan_out_changes_to_other_connections(db, user_id, None, _new_watched, _new_ratings, settings=settings, exclude_cloud_source=CollectionSource.trakt)
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats=stats,
                    processed_items=watched_processed,
                )
            )
            await db.commit()

        except Exception as exc:
            print(f"Trakt sync job {job_id} failed: {exc}")
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
    _tmdb_key = settings.tmdb_api_key
    if not _tmdb_key:
        _gs_r = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        _gs = _gs_r.scalar_one_or_none()
        _tmdb_key = _gs.tmdb_api_key if _gs else None
    if not _tmdb_key:
        raise HTTPException(status_code=400, detail="TMDB API key required for sync")

    job = SyncJob(user_id=current_user.id, source=CollectionSource.trakt, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_trakt_sync, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "Trakt sync is running in the background"}


async def _run_trakt_push(user_id: int, job_id: int) -> None:
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running)
            )
            await db.commit()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            if not settings or not settings.trakt_access_token or not settings.trakt_client_id:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message="Trakt is not connected"))
                await db.commit()
                return

            all_media_ids: set[int] = set()
            watched_ids: set[int] = set()
            ratings_map: RatingChanges = {}

            if settings.trakt_push_watched:
                watched_result = await db.execute(
                    select(WatchEvent.media_id).where(WatchEvent.user_id == user_id).distinct()
                )
                watched_ids = {row[0] for row in watched_result.all()}
                all_media_ids |= watched_ids

            if settings.trakt_push_ratings:
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
                all_media_ids |= {media_id for media_id, _ in ratings_map}

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

            push_tasks = []

            if settings.trakt_push_watched:
                for mid in watched_ids:
                    media = media_by_id.get(mid)
                    if not media or not media.tmdb_id:
                        continue
                    if media.media_type == MediaType.movie:
                        push_tasks.append(trakt_client.add_movie_to_history(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id))
                    elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                        show = shows_by_id.get(media.show_id)
                        if show and show.tmdb_id:
                            push_tasks.append(trakt_client.add_episode_to_history(settings.trakt_client_id, settings.trakt_access_token, show.tmdb_id, media.season_number, media.episode_number))

            if settings.trakt_push_ratings:
                from routers.sync import _get_effective_tmdb_key, _resolve_tmdb_season_ids

                season_tmdb_ids = await _resolve_tmdb_season_ids(
                    media_by_id,
                    set(ratings_map),
                    await _get_effective_tmdb_key(db, settings),
                )
                for key, rating in ratings_map.items():
                    mid, season_number = key
                    media = media_by_id.get(mid)
                    if not media or not media.tmdb_id:
                        continue
                    if season_number is not None:
                        if season_tmdb_id := season_tmdb_ids.get(key):
                            push_tasks.append(
                                trakt_client.set_season_rating(
                                    settings.trakt_client_id,
                                    settings.trakt_access_token,
                                    season_tmdb_id,
                                    rating,
                                )
                            )
                    elif media.media_type == MediaType.movie:
                        push_tasks.append(trakt_client.set_movie_rating(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id, rating))
                    elif media.media_type == MediaType.series:
                        push_tasks.append(trakt_client.set_show_rating(settings.trakt_client_id, settings.trakt_access_token, media.tmdb_id, rating))

            total = len(push_tasks)
            if not push_tasks:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats={"succeeded": 0, "failed": 0}, processed_items=0))
                await db.commit()
                return

            print(f"Trakt full push: pushing {total} items...")
            BATCH_SIZE = 50
            succeeded = 0
            failed = 0
            for i in range(0, total, BATCH_SIZE):
                batch = push_tasks[i:i + BATCH_SIZE]
                results = await asyncio.gather(*batch, return_exceptions=True)
                succeeded += sum(1 for r in results if not isinstance(r, Exception))
                failed    += sum(1 for r in results if isinstance(r, Exception))
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=succeeded + failed))
                await db.commit()
            print(f"Trakt full push: {succeeded}/{total} succeeded")

            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats={"succeeded": succeeded, "failed": failed},
                    processed_items=succeeded + failed,
                )
            )
            await db.commit()

        except Exception as exc:
            print(f"Trakt push job {job_id} failed: {exc}")
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=str(exc)))
            await db.commit()


@router.post("/push")
async def push_trakt(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    _require_trakt_config(settings)
    if not settings or not settings.trakt_access_token:
        raise HTTPException(status_code=400, detail="Trakt is not connected")
    if not settings.trakt_push_watched and not settings.trakt_push_ratings:
        raise HTTPException(status_code=400, detail="Enable 'Scrob → Trakt' push flags first")
    job = SyncJob(user_id=current_user.id, source=CollectionSource.trakt, status=SyncStatus.pending, job_type="push")
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(_run_trakt_push, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "Trakt push is running in the background"}
