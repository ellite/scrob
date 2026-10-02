"""WeTrakr integration router.

Endpoints:
  POST   /wetrakr/auth/device/start  – Start device auth flow
  POST   /wetrakr/auth/device/poll   – Poll for token completion
  DELETE /wetrakr/auth/disconnect    – Revoke token and clear stored credentials
  POST   /wetrakr/sync               – Trigger a WeTrakr import (watched history + ratings)
  POST   /wetrakr/push               – Push Scrob history/ratings to WeTrakr

Unlike Trakt/Simkl there's no per-user client_id/secret to configure — Scrob
ships a single app-owned key (core/wetrakr.py: WETRAKR_CLIENT_ID), so every
endpoint here only ever gates on whether the user is connected.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import wetrakr as wetrakr_client
from core.enrichment import enrich_media, is_unmapped_tvdb_episode, create_media_safely
from core.rewatch import record_rewatch_progress
from db import get_db, engine
from dependencies import get_current_user
from models.base import CollectionSource, MediaType
from models.comments import Comment
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
WETRAKR_PUSH_BATCH_SIZE = 50

# WeTrakr device-auth access tokens live 7 days. Refresh once we're within
# this of expiry so a job that starts near the boundary doesn't race it —
# same rationale as Trakt's _TRAKT_TOKEN_REFRESH_SKEW (see routers/trakt.py).
_WETRAKR_TOKEN_REFRESH_SKEW = timedelta(days=1)


class WeTrakrTokenError(Exception):
    """The stored WeTrakr token is unusable and can't be refreshed automatically -
    the user needs to reconnect WeTrakr in Settings. The message is safe to show."""


async def ensure_valid_wetrakr_token(
    db: AsyncSession, settings: UserSettings | None, *, force_check: bool = False
) -> str:
    """Return a usable WeTrakr access token, refreshing and persisting it first
    if it's expired or close to it. Raises WeTrakrTokenError when WeTrakr isn't
    connected or a needed refresh isn't possible / fails. Every path that calls
    WeTrakr with the stored token must go through this (see ensure_valid_trakt_token
    in routers/trakt.py for why — the scheduled push not doing so is a known trap)."""
    if not settings or not settings.wetrakr_access_token:
        raise WeTrakrTokenError("WeTrakr is not connected.")

    now = int(datetime.now(timezone.utc).timestamp())
    expires_at = settings.wetrakr_token_expires_at
    if not force_check and expires_at and expires_at - now > _WETRAKR_TOKEN_REFRESH_SKEW.total_seconds():
        return settings.wetrakr_access_token

    if await wetrakr_client.validate_token(settings.wetrakr_access_token):
        return settings.wetrakr_access_token

    if not settings.wetrakr_refresh_token:
        raise WeTrakrTokenError("WeTrakr token expired. Please reconnect WeTrakr in Settings.")

    try:
        token_data = await wetrakr_client.refresh_access_token(settings.wetrakr_refresh_token)
    except Exception as exc:
        logger.warning("WeTrakr token refresh failed for user %s: %s", settings.user_id, exc)
        raise WeTrakrTokenError(f"WeTrakr token expired and the automatic refresh failed: {exc}") from exc

    settings.wetrakr_access_token = token_data["access_token"]
    # WeTrakr's refresh endpoint returns the rotated refresh token under
    # new_refresh_token, not refresh_token — the docs flag this explicitly.
    settings.wetrakr_refresh_token = token_data.get("new_refresh_token") or token_data.get("refresh_token")
    settings.wetrakr_token_expires_at = token_data.get("expires_in", 0) + int(datetime.now(timezone.utc).timestamp())
    await db.commit()
    logger.info("Refreshed WeTrakr access token for user %s", settings.user_id)
    return settings.wetrakr_access_token


# ── Device Authentication ─────────────────────────────────────────────────────

@router.post("/auth/device/start")
async def wetrakr_device_start(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiate device authentication. Returns user_code + verification_url."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = UserSettings(user_id=current_user.id)
        db.add(settings)

    data = await wetrakr_client.start_device_auth()

    settings.wetrakr_device_code = data["device_code"]
    await db.commit()

    return {
        "user_code": data["user_code"],
        "verification_url": data["verification_url"],
        "expires_in": data["expires_in"],
        "interval": data["interval"],
    }


@router.post("/auth/device/poll")
async def wetrakr_device_poll(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check if the user has authorized the device. Call repeatedly per the interval."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if not settings or not settings.wetrakr_device_code:
        raise HTTPException(status_code=400, detail="No pending device authorization. Call /auth/device/start first.")

    try:
        token_data = await wetrakr_client.poll_device_token(settings.wetrakr_device_code)
    except Exception as exc:
        settings.wetrakr_device_code = None
        await db.commit()
        raise HTTPException(status_code=400, detail=f"Authorization failed: {exc}")

    if token_data is None:
        return {"status": "pending"}

    settings.wetrakr_access_token = token_data["access_token"]
    settings.wetrakr_refresh_token = token_data["refresh_token"]
    settings.wetrakr_token_expires_at = token_data.get("expires_in", 0) + int(datetime.now(timezone.utc).timestamp())
    settings.wetrakr_device_code = None
    await db.commit()

    return {"status": "connected"}


@router.delete("/auth/disconnect")
async def wetrakr_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revoke the WeTrakr session and clear stored credentials."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if settings and settings.wetrakr_access_token:
        if settings.wetrakr_refresh_token:
            await wetrakr_client.revoke_token(settings.wetrakr_access_token, settings.wetrakr_refresh_token)
        settings.wetrakr_access_token = None
        settings.wetrakr_refresh_token = None
        settings.wetrakr_token_expires_at = None
        settings.wetrakr_device_code = None
        await db.commit()

    return {"status": "disconnected"}


# ── Sync helpers (shared with run_wetrakr_sync) ───────────────────────────────

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
                "WeTrakr episode s%se%s not found on TMDB for show tmdb=%s — skipping",
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
            runtime=ep.get("runtime"),
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number,
            tmdb_data={"runtime": ep.get("runtime"), "cast": []},
        )
        return media
    except Exception as exc:
        logger.warning("Could not fetch episode s%se%s for show tmdb=%s: %s", season_number, episode_number, show_tmdb_id, exc)
        return None


def _parse_wetrakr_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from dateutil import parser as dt_parser
        dt = dt_parser.isoparse(value)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _wetrakr_media_tmdb_id(media_obj: dict) -> int | None:
    tmdb_id = (media_obj.get("ids") or {}).get("tmdb")
    return int(tmdb_id) if tmdb_id else None


def _wetrakr_user_rating(item: dict) -> tuple[float | None, str | None]:
    """Extract (rating, rated_at) from a /sync/ratings/{target} media object.

    interactions.user.rating is itself an object ({rating, rated_at}), not a
    bare number — the numeric value is nested one level further under its own
    "rating" key alongside "rated_at" (confirmed against the live API; the
    docs' wording is ambiguous enough that the flat-number reading seemed
    equally plausible and turned out wrong)."""
    user = ((item.get("interactions") or {}).get("user") or {})
    rating_obj = user.get("rating")
    if not isinstance(rating_obj, dict):
        return (None, None)
    rating = rating_obj.get("rating")
    return (float(rating) if rating else None, rating_obj.get("rated_at"))


async def _resolve_wetrakr_comment_target(
    comment: dict,
    caches: dict[str, dict[int, dict]],
) -> tuple[str, int, Optional[int], Optional[int]] | None:
    """Resolve a comment's compact target reference (WeTrakr-id only) to
    (media_type, tmdb_id, season_number, episode_number), matching the same
    convention Comment.tmdb_id already uses for Trakt imports (see
    _extract_trakt_comment): tmdb_id is the SHOW's id for season/episode
    comments, never the season/episode's own id.

    `caches` holds one dict per WeTrakr object type so repeated comments on
    the same title only fetch it once per sync run. Returns None for
    person/list targets (no Scrob equivalent) or when resolution fails.
    """
    target = comment.get("target")
    try:
        if target == "movie":
            wid = (comment.get("movie") or {}).get("id")
            if not wid:
                return None
            cache = caches.setdefault("movie", {})
            data = cache.get(wid) or await wetrakr_client.get_movie(wid)
            cache[wid] = data
            tmdb_id = (data.get("ids") or {}).get("tmdb")
            return ("movie", int(tmdb_id), None, None) if tmdb_id else None

        if target == "show":
            wid = (comment.get("show") or {}).get("id")
            if not wid:
                return None
            cache = caches.setdefault("show", {})
            data = cache.get(wid) or await wetrakr_client.get_show(wid)
            cache[wid] = data
            tmdb_id = (data.get("ids") or {}).get("tmdb")
            return ("series", int(tmdb_id), None, None) if tmdb_id else None

        if target == "season":
            wid = (comment.get("season") or {}).get("id")
            if not wid:
                return None
            cache = caches.setdefault("season", {})
            data = cache.get(wid) or await wetrakr_client.get_season(wid)
            cache[wid] = data
            show_tmdb_id = ((data.get("show") or {}).get("ids") or {}).get("tmdb")
            season_number = data.get("number")
            if not show_tmdb_id or season_number is None:
                return None
            return ("series", int(show_tmdb_id), int(season_number), None)

        if target == "episode":
            wid = (comment.get("episode") or {}).get("id")
            if not wid:
                return None
            cache = caches.setdefault("episode", {})
            data = cache.get(wid) or await wetrakr_client.get_episode(wid)
            cache[wid] = data
            show_tmdb_id = ((data.get("show") or {}).get("ids") or {}).get("tmdb")
            # Unconfirmed field name (see core.wetrakr.get_episode) — try the
            # flat field first, then a nested season object, before giving up.
            season_number = data.get("season_number")
            if season_number is None:
                season_number = (data.get("season") or {}).get("number")
            episode_number = data.get("number")
            if not show_tmdb_id or season_number is None or episode_number is None:
                return None
            return ("episode", int(show_tmdb_id), int(season_number), int(episode_number))
    except Exception as exc:
        logger.warning("Could not resolve WeTrakr comment target (%s, id=%s): %s", target, comment.get(target, {}).get("id") if isinstance(comment.get(target), dict) else None, exc)
        return None

    return None  # target is "person" or "list" — no Scrob equivalent


# ── Background sync job ───────────────────────────────────────────────────────

async def run_wetrakr_sync(user_id: int, job_id: int) -> None:
    from routers.sync import SyncCancelled, _raise_if_cancelled, _short_error
    print(f"Starting WeTrakr sync for user {user_id}, job {job_id}")
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        stats: dict[str, int] = {
            "movies": 0, "episodes": 0, "ratings": 0, "lists": 0, "list_items": 0,
            "comments": 0, "skipped": 0, "errors": 0,
        }
        try:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.running, processed_items=0, total_items=0, current_step="Pulling from WeTrakr"
                )
            )
            await db.commit()

            result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = result.scalar_one_or_none()

            if not settings or not settings.wetrakr_access_token:
                err = "WeTrakr is not connected"
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=err))
                await db.commit()
                return

            try:
                access_token = await ensure_valid_wetrakr_token(db, settings)
            except WeTrakrTokenError as exc:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=_short_error(exc)))
                await db.commit()
                return

            _gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
            _gs = _gs_result.scalar_one_or_none()
            api_key = settings.tmdb_api_key or (_gs.tmdb_api_key if _gs else None)

            we_res = await db.execute(select(WatchEvent.media_id).where(WatchEvent.user_id == user_id))
            existing_watched: set[int] = {row[0] for row in we_res}

            # ── Watched movies ────────────────────────────────────────────────
            if settings.wetrakr_sync_watched:
                print("  Fetching movie watch history from WeTrakr…")
                history_movies = await wetrakr_client.get_watched_history(access_token, "movies")
                print(f"  {len(history_movies)} movie plays fetched from WeTrakr")
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(history_movies), current_step="Pulling watched movies"))
                await db.commit()

                for i, item in enumerate(history_movies, start=1):
                    movie_data = item.get("movie", {})
                    tmdb_id = _wetrakr_media_tmdb_id(movie_data)
                    try:
                        if not tmdb_id:
                            stats["skipped"] += 1
                            continue
                        async with db.begin_nested():
                            media = await _get_or_create_movie_media(db, tmdb_id, movie_data.get("title", ""), api_key)
                            if not media:
                                stats["errors"] += 1
                                continue
                            if media.id not in existing_watched:
                                watched_at = None if item.get("watched_at_unknown") else _parse_wetrakr_datetime(item.get("watched_at"))
                                db.add(WatchEvent(
                                    user_id=user_id,
                                    media_id=media.id,
                                    watched_at=watched_at,
                                    completed=True,
                                    play_count=1,
                                ))
                                existing_watched.add(media.id)
                                stats["movies"] += 1
                            else:
                                stats["skipped"] += 1
                    except Exception as exc:
                        logger.warning("Error processing WeTrakr movie tmdb=%s: %s", tmdb_id, exc)
                        stats["errors"] += 1
                    finally:
                        if i % 25 == 0 or i == len(history_movies):
                            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=i))
                            await db.commit()
                            await _raise_if_cancelled(db, job_id)
                await db.commit()

            # ── Watched episodes ──────────────────────────────────────────────
            if settings.wetrakr_sync_watched:
                print("  Fetching episode watch history from WeTrakr…")
                history_episodes = await wetrakr_client.get_watched_history(access_token, "episodes")
                print(f"  {len(history_episodes)} episode plays fetched from WeTrakr")

                movies_count = stats["movies"] + stats["skipped"]
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                    total_items=movies_count + len(history_episodes),
                    processed_items=movies_count,
                    current_step="Pulling watched shows",
                ))
                await db.commit()

                plays_by_show: dict[int, list[dict]] = {}
                for entry in history_episodes:
                    ep_data = entry.get("episode", {})
                    show_data = ep_data.get("show", {})
                    show_tmdb_id = _wetrakr_media_tmdb_id(show_data)
                    if show_tmdb_id:
                        plays_by_show.setdefault(show_tmdb_id, []).append(entry)
                    else:
                        stats["skipped"] += 1

                processed = movies_count
                for show_tmdb_id, entries in plays_by_show.items():
                    show_title = entries[0].get("episode", {}).get("show", {}).get("title", "")
                    try:
                        async with db.begin_nested():
                            show = await _get_or_create_show(db, show_tmdb_id, show_title, api_key)
                            if not show:
                                stats["errors"] += 1
                                processed += len(entries)
                                continue
                            await db.flush()

                        for entry in entries:
                            ep_data = entry.get("episode", {})
                            season_num = ep_data.get("season_number")
                            ep_num = ep_data.get("number")
                            if season_num is None or ep_num is None:
                                stats["skipped"] += 1
                                continue
                            try:
                                async with db.begin_nested():
                                    media = await _get_or_create_episode_media(db, show.id, show_tmdb_id, season_num, ep_num, api_key)
                                    if not media:
                                        stats["errors"] += 1
                                        continue
                                    if media.id not in existing_watched:
                                        watched_at = None if entry.get("watched_at_unknown") else _parse_wetrakr_datetime(entry.get("watched_at"))
                                        event = WatchEvent(
                                            user_id=user_id,
                                            media_id=media.id,
                                            watched_at=watched_at,
                                            completed=True,
                                            play_count=1,
                                        )
                                        db.add(event)
                                        await db.flush()
                                        await record_rewatch_progress(db, user_id, media.id, event.id)
                                        existing_watched.add(media.id)
                                        stats["episodes"] += 1
                                    else:
                                        stats["skipped"] += 1
                            except Exception as exc:
                                logger.warning("Error processing episode s%se%s for show tmdb=%s: %s", season_num, ep_num, show_tmdb_id, exc)
                                stats["errors"] += 1
                    except Exception as exc:
                        logger.warning("Error processing WeTrakr show tmdb=%s: %s", show_tmdb_id, exc)
                        stats["errors"] += 1
                    finally:
                        processed += len(entries)
                        await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=processed))
                        await db.commit()
                        await _raise_if_cancelled(db, job_id)

            # ── Ratings (movies + shows only, see set_ratings_batch) ──────────
            if settings.wetrakr_sync_ratings:
                print("  Fetching ratings from WeTrakr…")
                rat_res = await db.execute(
                    select(Rating.media_id).where(
                        Rating.user_id == user_id,
                        Rating.season_number.is_(None),
                        Rating.episode_order.is_(None),
                    )
                )
                existing_rated: set[int] = {row[0] for row in rat_res}

                for target, get_media in (
                    ("movies", _get_or_create_movie_media),
                    ("shows", _get_or_create_series_media),
                ):
                    try:
                        items = await wetrakr_client.get_ratings(access_token, target)
                    except Exception as exc:
                        logger.warning("Failed to fetch WeTrakr %s ratings: %s", target, exc)
                        continue
                    for item in items:
                        tmdb_id = _wetrakr_media_tmdb_id(item)
                        rating_val, rated_at_raw = _wetrakr_user_rating(item)
                        if not tmdb_id or not rating_val:
                            continue
                        try:
                            async with db.begin_nested():
                                media = await get_media(db, tmdb_id, item.get("title", ""), api_key)
                                if not media:
                                    stats["errors"] += 1
                                    continue
                                if media.id not in existing_rated:
                                    db.add(Rating(
                                        user_id=user_id,
                                        media_id=media.id,
                                        rating=rating_val,
                                        rated_at=_parse_wetrakr_datetime(rated_at_raw) or datetime.utcnow(),
                                    ))
                                    existing_rated.add(media.id)
                                    stats["ratings"] += 1
                        except Exception as exc:
                            logger.warning("Error processing WeTrakr %s rating tmdb=%s: %s", target, tmdb_id, exc)
                            stats["errors"] += 1
                await db.commit()
                await _raise_if_cancelled(db, job_id)

            # ── Lists (movie/show items only, see add_items_to_list) ──────────
            if settings.wetrakr_sync_lists:
                print("  Fetching lists from WeTrakr…")
                try:
                    remote_lists = await wetrakr_client.get_lists(access_token)
                except Exception as exc:
                    logger.warning("Failed to fetch WeTrakr lists: %s", exc)
                    remote_lists = []

                for wlist in remote_lists:
                    remote_id = wlist.get("id")
                    if not remote_id:
                        continue
                    try:
                        existing_list_result = await db.execute(
                            select(ListModel).where(ListModel.user_id == user_id, ListModel.wetrakr_list_id == remote_id)
                        )
                        local_list = existing_list_result.scalar_one_or_none()
                        if not local_list:
                            local_list = ListModel(
                                user_id=user_id,
                                name=wlist.get("name") or "WeTrakr list",
                                description=wlist.get("description"),
                                wetrakr_list_id=remote_id,
                            )
                            db.add(local_list)
                            await db.flush()
                            stats["lists"] += 1
                        else:
                            local_list.name = wlist.get("name") or local_list.name
                            local_list.description = wlist.get("description")

                        existing_items_result = await db.execute(
                            select(ListItem.media_id).where(ListItem.list_id == local_list.id, ListItem.season_number.is_(None))
                        )
                        existing_item_ids: set[int] = {row[0] for row in existing_items_result}

                        try:
                            items = await wetrakr_client.get_list_items(access_token, remote_id)
                        except Exception as exc:
                            logger.warning("Could not fetch items for WeTrakr list %s: %s", remote_id, exc)
                            continue

                        for entry in items:
                            item_type = entry.get("type")
                            media: Media | None = None
                            tmdb_id = _wetrakr_media_tmdb_id(entry)
                            if not tmdb_id or item_type not in ("movie", "show"):
                                # season/episode/person list items need
                                # WeTrakr-native id resolution, not built yet.
                                stats["skipped"] += 1
                                continue
                            async with db.begin_nested():
                                if item_type == "movie":
                                    media = await _get_or_create_movie_media(db, tmdb_id, entry.get("title", ""), api_key)
                                else:
                                    media = await _get_or_create_series_media(db, tmdb_id, entry.get("title", ""), api_key)
                            if media and media.id not in existing_item_ids:
                                db.add(ListItem(list_id=local_list.id, media_id=media.id))
                                existing_item_ids.add(media.id)
                                stats["list_items"] += 1
                    except Exception as exc:
                        logger.warning("Error processing WeTrakr list %s: %s", remote_id, exc)
                        stats["errors"] += 1
                await db.commit()
                await _raise_if_cancelled(db, job_id)

            # ── Comments (movies/shows/seasons/episodes, see write_comment) ──
            if settings.wetrakr_sync_comments:
                print("  Fetching comments from WeTrakr…")
                existing_comments_result = await db.execute(select(Comment).where(Comment.user_id == user_id))
                existing_comment_keys = {
                    (c.media_type, c.tmdb_id, c.season_number, c.episode_number, c.content)
                    for c in existing_comments_result.scalars().all()
                }
                lookup_caches: dict[str, dict[int, dict]] = {}
                for target in ("movies", "shows", "seasons", "episodes"):
                    try:
                        entries = await wetrakr_client.get_comments(access_token, target)
                    except Exception as exc:
                        logger.warning("Failed to fetch WeTrakr %s comments: %s", target, exc)
                        continue
                    for entry in entries:
                        resolved = await _resolve_wetrakr_comment_target(entry, lookup_caches)
                        if not resolved:
                            stats["skipped"] += 1
                            continue
                        media_type, tmdb_id, season_number, episode_number = resolved
                        content = entry.get("text") or ""
                        key = (media_type, tmdb_id, season_number, episode_number, content)
                        if key in existing_comment_keys:
                            stats["skipped"] += 1
                            continue
                        try:
                            async with db.begin_nested():
                                db.add(Comment(
                                    user_id=user_id,
                                    media_type=media_type,
                                    tmdb_id=tmdb_id,
                                    season_number=season_number,
                                    episode_number=episode_number,
                                    content=content,
                                    is_spoiler=bool(entry.get("spoiler")),
                                    created_at=_parse_wetrakr_datetime(entry.get("comment_added_at")) or datetime.utcnow(),
                                    wetrakr_comment_id=entry.get("id"),
                                ))
                            existing_comment_keys.add(key)
                            stats["comments"] += 1
                        except Exception as exc:
                            logger.warning("Error saving WeTrakr comment (tmdb=%s): %s", tmdb_id, exc)
                            stats["errors"] += 1
                await db.commit()
                await _raise_if_cancelled(db, job_id)

            print(
                f"WeTrakr sync job {job_id} completed. "
                f"Movies: {stats['movies']} new. Episodes: {stats['episodes']} new. "
                f"Ratings: {stats['ratings']} new. Lists: {stats['lists']} new, {stats['list_items']} items. "
                f"Comments: {stats['comments']} new. Skipped: {stats['skipped']}. Errors: {stats['errors']}."
            )
            # A pull only populates scrob's own data — never auto-pushed elsewhere.
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats=stats,
                    processed_items=stats["movies"] + stats["episodes"] + stats["ratings"] + stats["list_items"] + stats["comments"],
                )
            )
            await db.commit()

        except SyncCancelled:
            print(f"WeTrakr sync job {job_id} cancelled")
            await db.rollback()  # session may be poisoned by a failed flush — see the Exception branch below
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.cancelled, stats=stats))
            await db.commit()

        except Exception as exc:
            print(f"WeTrakr sync job {job_id} failed: {exc}")
            # A DB error mid-loop leaves the session unusable for any further
            # query until rolled back (PendingRollbackError) — without this,
            # marking the job failed here would itself raise and the job
            # would stay stuck "running" forever (#comments int32 incident).
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=_short_error(exc)))
            await db.commit()


@router.post("/sync")
async def sync_wetrakr(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if not settings or not settings.wetrakr_access_token:
        raise HTTPException(status_code=400, detail="WeTrakr is not connected")

    _tmdb_key = settings.tmdb_api_key if settings else None
    if not _tmdb_key:
        _gs_r = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        _gs = _gs_r.scalar_one_or_none()
        _tmdb_key = _gs.tmdb_api_key if _gs else None
    if not _tmdb_key:
        raise HTTPException(status_code=400, detail="TMDB API key required for sync")

    job = SyncJob(user_id=current_user.id, source=CollectionSource.wetrakr, status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_wetrakr_sync, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "WeTrakr sync is running in the background"}


# ── Push (Scrob → WeTrakr) ─────────────────────────────────────────────────────

async def _run_wetrakr_push(user_id: int, job_id: int) -> None:
    from routers.sync import SyncCancelled, _raise_if_cancelled, _select_in_chunks, _short_error, _latest_watched_at
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        succeeded = 0
        failed = 0
        try:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running))
            await db.commit()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            if not settings or not settings.wetrakr_access_token:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message="WeTrakr is not connected"))
                await db.commit()
                return

            try:
                access_token = await ensure_valid_wetrakr_token(db, settings)
            except WeTrakrTokenError as exc:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=_short_error(exc)))
                await db.commit()
                return

            all_media_ids: set[int] = set()
            watched_ids: set[int] = set()
            ratings_map: dict[int, float] = {}

            if settings.wetrakr_push_watched:
                watched_result = await db.execute(
                    select(WatchEvent.media_id).where(WatchEvent.user_id == user_id).distinct()
                )
                watched_ids = {row[0] for row in watched_result.all()}
                all_media_ids |= watched_ids
                watched_at_by_media = await _latest_watched_at(db, user_id, list(watched_ids))

            if settings.wetrakr_push_ratings:
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

            if not all_media_ids and not settings.wetrakr_push_lists and not settings.wetrakr_push_comments:
                await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats={"succeeded": 0, "failed": 0}, processed_items=0, total_items=0))
                await db.commit()
                return

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(all_media_ids)))
            await db.commit()

            media_rows = await _select_in_chunks(db, lambda chunk: select(Media).where(Media.id.in_(chunk)), list(all_media_ids))
            media_by_id: dict[int, Media] = {m.id: m for m in media_rows}

            show_ids = {m.show_id for m in media_by_id.values() if m.show_id}
            shows_by_id: dict[int, Show] = {}
            if show_ids:
                show_rows = await _select_in_chunks(db, lambda chunk: select(Show).where(Show.id.in_(chunk)), list(show_ids))
                shows_by_id = {s.id: s for s in show_rows}

            movie_candidates: list[tuple[int, datetime]] = []
            episode_candidates: list[tuple[int, int, int, datetime]] = []

            if settings.wetrakr_push_watched:
                for mid in watched_ids:
                    media = media_by_id.get(mid)
                    if not media or not media.tmdb_id or is_unmapped_tvdb_episode(media):
                        continue
                    watched_at = watched_at_by_media.get(mid)
                    if media.media_type == MediaType.movie:
                        movie_candidates.append((media.tmdb_id, watched_at))
                    elif media.media_type == MediaType.episode and media.show_id and media.season_number is not None and media.episode_number is not None:
                        show = shows_by_id.get(media.show_id)
                        if show and show.tmdb_id:
                            episode_candidates.append((show.tmdb_id, media.season_number, media.episode_number, watched_at))
                movie_candidates.sort(key=lambda item: item[1] or datetime.min)
                episode_candidates.sort(key=lambda item: item[3] or datetime.min)

            movie_rating_candidates: list[tuple[int, float]] = []
            show_rating_candidates: list[tuple[int, float]] = []

            if settings.wetrakr_push_ratings:
                for mid, rating in ratings_map.items():
                    media = media_by_id.get(mid)
                    if not media or not media.tmdb_id or is_unmapped_tvdb_episode(media):
                        continue
                    if media.media_type == MediaType.movie:
                        movie_rating_candidates.append((media.tmdb_id, rating))
                    elif media.media_type == MediaType.series:
                        show_rating_candidates.append((media.tmdb_id, rating))
                    # Episode/season ratings have no push path yet — see
                    # core/wetrakr.py: set_ratings_batch.

            push_tasks: list[tuple[str, int, "asyncio.Future"]] = []

            history_pending: list[tuple[str, tuple]] = [("movie", item) for item in movie_candidates]
            history_pending.extend(("episode", item) for item in episode_candidates)
            for i in range(0, len(history_pending), WETRAKR_PUSH_BATCH_SIZE):
                chunk = history_pending[i:i + WETRAKR_PUSH_BATCH_SIZE]
                movies = [item for kind, item in chunk if kind == "movie"]
                episodes = [item for kind, item in chunk if kind == "episode"]
                push_tasks.append(("watched", len(chunk), wetrakr_client.add_to_watched_batch(access_token, movies, episodes)))

            rating_pending: list[tuple[str, tuple]] = [("movie", item) for item in movie_rating_candidates]
            rating_pending.extend(("show", item) for item in show_rating_candidates)
            for i in range(0, len(rating_pending), WETRAKR_PUSH_BATCH_SIZE):
                chunk = rating_pending[i:i + WETRAKR_PUSH_BATCH_SIZE]
                movie_ratings = [item for kind, item in chunk if kind == "movie"]
                show_ratings = [item for kind, item in chunk if kind == "show"]
                push_tasks.append(("ratings", len(chunk), wetrakr_client.set_ratings_batch(access_token, movie_ratings, show_ratings)))

            total = sum(item_count for _, item_count, _ in push_tasks)

            if push_tasks:
                print(f"WeTrakr full push: pushing {total} items in {len(push_tasks)} batch requests…")
                REQUEST_CONCURRENCY = 50
                for i in range(0, len(push_tasks), REQUEST_CONCURRENCY):
                    batch = push_tasks[i:i + REQUEST_CONCURRENCY]
                    results = await asyncio.gather(*[task for _, _, task in batch], return_exceptions=True)
                    for (category, item_count, _), result in zip(batch, results):
                        if isinstance(result, Exception):
                            failed += item_count
                            logger.warning("WeTrakr push batch failed (%s, %d items): %s", category, item_count, result)
                        else:
                            succeeded += item_count
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=succeeded + failed))
                    await db.commit()
                    await _raise_if_cancelled(db, job_id)
                print(f"WeTrakr full push: {succeeded}/{total} succeeded")

            # ── Lists (movie/show items only — see add_items_to_list) ────────
            # Idempotent server-side, so safe to re-run: an already-linked
            # list's items are just re-added (a no-op for ones already there).
            if settings.wetrakr_push_lists:
                lists_result = await db.execute(select(ListModel).where(ListModel.user_id == user_id))
                local_lists = lists_result.scalars().all()
                if local_lists:
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=len(all_media_ids) + len(local_lists)))
                    await db.commit()
                for local_list in local_lists:
                    try:
                        remote_id = local_list.wetrakr_list_id
                        if not remote_id:
                            created = await wetrakr_client.create_list(access_token, local_list.name, local_list.description)
                            remote_id = created.get("id")
                            if not remote_id:
                                raise ValueError("WeTrakr did not return a list id")
                            local_list.wetrakr_list_id = remote_id
                            await db.commit()

                        items_result = await db.execute(
                            select(Media.tmdb_id, Media.media_type)
                            .join(ListItem, ListItem.media_id == Media.id)
                            .where(ListItem.list_id == local_list.id, ListItem.season_number.is_(None))
                        )
                        movie_tmdb_ids: list[int] = []
                        show_tmdb_ids: list[int] = []
                        for tmdb_id, media_type in items_result.all():
                            if not tmdb_id:
                                continue
                            if media_type == MediaType.movie:
                                movie_tmdb_ids.append(tmdb_id)
                            elif media_type == MediaType.series:
                                show_tmdb_ids.append(tmdb_id)
                        if movie_tmdb_ids or show_tmdb_ids:
                            await wetrakr_client.add_items_to_list(access_token, remote_id, movie_tmdb_ids, show_tmdb_ids)
                        succeeded += 1
                    except Exception as exc:
                        failed += 1
                        logger.warning("WeTrakr list push failed for list %s: %s", local_list.id, exc)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=succeeded + failed))
                    await db.commit()
                    await _raise_if_cancelled(db, job_id)

            # ── Comments (movie/show only — see write_comment) ───────────────
            # Only ever considers comments with no wetrakr_comment_id yet:
            # WeTrakr has no edit/upsert for comments, so anything already
            # linked (pulled in, or pushed by an earlier run) is left alone.
            if settings.wetrakr_push_comments:
                comments_result = await db.execute(
                    select(Comment).where(
                        Comment.user_id == user_id,
                        Comment.wetrakr_comment_id.is_(None),
                        Comment.tmdb_id.isnot(None),
                        Comment.media_type.in_(["movie", "series"]),
                        Comment.season_number.is_(None),
                    )
                )
                pending_comments = comments_result.scalars().all()
                if pending_comments:
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                        total_items=len(all_media_ids) + (len(local_lists) if settings.wetrakr_push_lists else 0) + len(pending_comments)
                    ))
                    await db.commit()
                for comment in pending_comments:
                    try:
                        created = await wetrakr_client.write_comment(
                            access_token,
                            movie_tmdb_id=comment.tmdb_id if comment.media_type == "movie" else None,
                            show_tmdb_id=comment.tmdb_id if comment.media_type == "series" else None,
                            text=comment.content,
                            spoiler=comment.is_spoiler,
                        )
                        comment.wetrakr_comment_id = created.get("id")
                        await db.commit()
                        succeeded += 1
                    except Exception as exc:
                        failed += 1
                        logger.warning("WeTrakr comment push failed for comment %s: %s", comment.id, exc)
                    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=succeeded + failed))
                    await db.commit()
                    await _raise_if_cancelled(db, job_id)

            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    stats={"succeeded": succeeded, "failed": failed},
                    processed_items=succeeded + failed,
                )
            )
            await db.commit()

        except SyncCancelled:
            print(f"WeTrakr push job {job_id} cancelled")
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                status=SyncStatus.cancelled,
                stats={"succeeded": succeeded, "failed": failed},
                processed_items=succeeded + failed,
            ))
            await db.commit()

        except Exception as exc:
            print(f"WeTrakr push job {job_id} failed: {exc}")
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=_short_error(exc)))
            await db.commit()


@router.post("/push")
async def push_wetrakr(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()
    if not settings or not settings.wetrakr_access_token:
        raise HTTPException(status_code=400, detail="WeTrakr is not connected")
    if not any([settings.wetrakr_push_watched, settings.wetrakr_push_ratings, settings.wetrakr_push_lists, settings.wetrakr_push_comments]):
        raise HTTPException(status_code=400, detail="Enable 'Scrob → WeTrakr' push flags first")
    job = SyncJob(user_id=current_user.id, source=CollectionSource.wetrakr, status=SyncStatus.pending, job_type="push")
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(_run_wetrakr_push, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "WeTrakr push is running in the background"}
