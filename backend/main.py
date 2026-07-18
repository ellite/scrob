import asyncio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from db import engine, Base
import models # noqa: F401
from routers import webhooks, media, history, ratings, sync, shows, auth, lists, oidc, profile, trakt, simkl, mdblist, comments, admin, compat

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from core.limiter import limiter

from sqlalchemy import or_, select, update, delete
from models.sync import SyncJob, SyncStatus
from models.base import CollectionSource
from models.playback_session import PlaybackSession


async def _auto_sync_scheduler():
    from db import async_sessionmaker
    from models.connections import MediaServerConnection
    from routers.sync import run_jellyfin_sync, run_emby_sync, run_plex_sync, run_nuvio_sync
    from datetime import datetime, timezone

    CHECK_INTERVAL = 300  # seconds between scheduler ticks

    source_map = {"jellyfin": CollectionSource.jellyfin, "emby": CollectionSource.emby, "plex": CollectionSource.plex, "nuvio": CollectionSource.nuvio}
    runner_map = {"jellyfin": run_jellyfin_sync, "emby": run_emby_sync, "plex": run_plex_sync, "nuvio": run_nuvio_sync}

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with async_session() as db:
                result = await db.execute(
                    select(MediaServerConnection).where(
                        MediaServerConnection.auto_sync_interval.isnot(None)
                    )
                )
                connections = result.scalars().all()

                now = datetime.now(timezone.utc).replace(tzinfo=None)

                for conn in connections:
                    user_id = conn.user_id
                    source = source_map.get(conn.type)
                    run_fn = runner_map.get(conn.type)
                    if not source or not run_fn:
                        continue

                    # Skip if a sync is already pending or running for this connection
                    active_q = await db.execute(
                        select(SyncJob).where(
                            SyncJob.user_id == user_id,
                            SyncJob.source == source,
                            SyncJob.connection_id == conn.id,
                            SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]),
                        )
                    )
                    if active_q.scalar_one_or_none():
                        continue

                    # Find the last completed or failed sync for this user+source+connection
                    last_q = await db.execute(
                        select(SyncJob).where(
                            SyncJob.user_id == user_id,
                            SyncJob.source == source,
                            SyncJob.connection_id == conn.id,
                            SyncJob.status.in_([SyncStatus.completed, SyncStatus.failed]),
                        ).order_by(SyncJob.updated_at.desc()).limit(1)
                    )
                    last_job = last_q.scalar_one_or_none()

                    if last_job:
                        elapsed_hours = (now - last_job.updated_at).total_seconds() / 3600
                        if elapsed_hours < conn.auto_sync_interval:
                            continue

                    job = SyncJob(user_id=user_id, source=source, status=SyncStatus.pending, connection_id=conn.id)
                    db.add(job)
                    await db.flush()
                    job_id = job.id
                    await db.commit()

                    print(f"Auto-sync: queuing {conn.type} sync for user {user_id}, connection {conn.id} (job {job_id})")
                    asyncio.create_task(run_fn(user_id, job_id, 0, 0, conn.id))

        except Exception as e:
            print(f"Auto-sync scheduler error: {e}")
            import traceback
            traceback.print_exc()


async def _manual_session_completer():
    from db import async_sessionmaker
    from routers.history import auto_complete_manual_sessions

    while True:
        await asyncio.sleep(60)
        try:
            async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with async_session() as db:
                await auto_complete_manual_sessions(db)
        except Exception as e:
            print(f"Manual session completer error: {e}")


async def _watchlist_poller():
    import logging
    log = logging.getLogger("uvicorn.error")

    try:
        from db import async_sessionmaker
        from models.connections import MediaServerConnection
        from models.users import UserSettings
        from models.global_settings import GlobalSettings
        from routers.media import _effective_radarr, _effective_sonarr
        from core import plex as plex_client
        from core import radarr as radarr_client
        from core import sonarr as sonarr_client
    except Exception as e:
        log.error(f"Watchlist poller: failed to import dependencies: {e}")
        return

    CHECK_INTERVAL = 300
    log.info("Watchlist poller: started")

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with async_session() as db:
                result = await db.execute(
                    select(MediaServerConnection).where(
                        MediaServerConnection.type == "plex",
                        or_(
                            MediaServerConnection.watchlist_to_radarr.is_(True),
                            MediaServerConnection.watchlist_to_sonarr.is_(True),
                        ),
                    )
                )
                connections = result.scalars().all()

                for conn in connections:
                    try:
                        settings_q = await db.execute(
                            select(UserSettings).where(UserSettings.user_id == conn.user_id)
                        )
                        user_settings = settings_q.scalar_one_or_none()
                        gs_q = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
                        global_settings = gs_q.scalar_one_or_none()

                        radarr_cfg = _effective_radarr(user_settings, global_settings) if conn.watchlist_to_radarr else None
                        sonarr_cfg = _effective_sonarr(user_settings, global_settings) if conn.watchlist_to_sonarr else None

                        if not radarr_cfg and not sonarr_cfg:
                            log.info(f"Watchlist poller: connection {conn.id} — Radarr/Sonarr not configured, skipping")
                            continue

                        synced: set = set(conn.watchlist_synced_ids or [])
                        newly_synced: set = set()

                        async def _send_to_arr(item_type: str, guids, title: str, cache_key: str):
                            """Send one item to Radarr or Sonarr and mark it synced."""
                            tmdb_id = plex_client.extract_tmdb_id(guids)
                            if not tmdb_id:
                                return
                            if cache_key in synced or cache_key in newly_synced:
                                return
                            if item_type == "movie" and radarr_cfg:
                                try:
                                    await radarr_client.add_movie(
                                        url=radarr_cfg.radarr_url,
                                        token=radarr_cfg.radarr_token,
                                        tmdb_id=tmdb_id,
                                        title=title,
                                        root_folder=radarr_cfg.radarr_root_folder,
                                        quality_profile_id=radarr_cfg.radarr_quality_profile,
                                        tags=radarr_cfg.radarr_tags,
                                    )
                                    newly_synced.add(cache_key)
                                    log.info(f"Watchlist: queued movie tmdb:{tmdb_id} in Radarr for user {conn.user_id}")
                                except Exception as e:
                                    log.error(f"Watchlist: Radarr error for tmdb:{tmdb_id}: {e}")
                            elif item_type == "show" and sonarr_cfg:
                                tvdb_id = plex_client.extract_tvdb_id(guids)
                                if not tvdb_id:
                                    return
                                try:
                                    await sonarr_client.add_series(
                                        url=sonarr_cfg.sonarr_url,
                                        token=sonarr_cfg.sonarr_token,
                                        tvdb_id=int(tvdb_id),
                                        root_folder=sonarr_cfg.sonarr_root_folder,
                                        quality_profile_id=sonarr_cfg.sonarr_quality_profile,
                                        tags=sonarr_cfg.sonarr_tags,
                                        season_folder=sonarr_cfg.sonarr_season_folder if sonarr_cfg.sonarr_season_folder is not None else True,
                                    )
                                    newly_synced.add(cache_key)
                                    log.info(f"Watchlist: queued show tvdb:{tvdb_id} in Sonarr for user {conn.user_id}")
                                except Exception as e:
                                    log.error(f"Watchlist: Sonarr error for tvdb:{tvdb_id}: {e}")

                        # Admin's own watchlist via REST (returns GUIDs directly)
                        own_watchlist = await plex_client.get_watchlist(conn.token)
                        for item in own_watchlist:
                            item_type = item.get("type")
                            guids = plex_client.get_guids(item)
                            tmdb_id = plex_client.extract_tmdb_id(guids)
                            if not tmdb_id:
                                continue
                            cache_key = f"{item_type}:{tmdb_id}"
                            await _send_to_arr(item_type, guids, item.get("title", ""), cache_key)

                        # Friends' watchlists via GraphQL (requires per-item enrichment for GUIDs)
                        if conn.watchlist_all_users:
                            all_friends = await plex_client.get_all_friends(conn.token)
                            monitored = set(conn.watchlist_monitored_users or [])
                            friends = [f for f in all_friends if f["watchlist_id"] in monitored] if monitored else []
                            for friend in friends:
                                friend_items = await plex_client.get_friend_watchlist(conn.token, friend["watchlist_id"])
                                for fi in friend_items:
                                    plex_id = fi.get("id")
                                    if not plex_id:
                                        continue
                                    cache_key = f"plex:{plex_id}"
                                    if cache_key in synced or cache_key in newly_synced:
                                        continue
                                    enriched = await plex_client.enrich_plex_item(conn.token, plex_id)
                                    if not enriched:
                                        continue
                                    item_type = fi.get("type", "").lower()
                                    guids = plex_client.get_guids(enriched)
                                    await _send_to_arr(item_type, guids, fi.get("title", ""), cache_key)

                        if newly_synced:
                            conn.watchlist_synced_ids = list(synced | newly_synced)
                            await db.commit()

                    except Exception as e:
                        log.error(f"Watchlist poller: error on connection {conn.id}: {e}", exc_info=True)

        except Exception as e:
            log.error(f"Watchlist poller error: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Clean up stuck sync jobs and orphaned playback sessions on startup
    from db import async_sessionmaker
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        await db.execute(
            update(SyncJob)
            .where(SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]))
            .values(status=SyncStatus.failed, error_message="Aborted due to server restart")
        )
        await db.execute(delete(PlaybackSession))
        await db.commit()

    scheduler_task = asyncio.create_task(_auto_sync_scheduler())
    watchlist_task = asyncio.create_task(_watchlist_poller())
    manual_session_task = asyncio.create_task(_manual_session_completer())

    yield

    scheduler_task.cancel()
    watchlist_task.cancel()
    manual_session_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    try:
        await watchlist_task
    except asyncio.CancelledError:
        pass
    try:
        await manual_session_task
    except asyncio.CancelledError:
        pass

from core.config import settings

# Rate limiter — keyed by client IP, in-memory storage (suitable for single-instance deploy).
app = FastAPI(title="Scrob", version="0.1.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# The backend is internal-only (localhost), but lock CORS to the configured
# frontend origin as defence-in-depth. The backend uses Bearer token auth only
# (no cookies), so allow_credentials is not needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.server_url],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(oidc.router, prefix="/auth/oidc", tags=["oidc"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(media.router, prefix="/media", tags=["media"])
app.include_router(history.router, prefix="/history", tags=["history"])
app.include_router(ratings.router, prefix="/ratings", tags=["ratings"])
app.include_router(sync.router, prefix="/sync", tags=["sync"])
app.include_router(shows.router, prefix="/shows", tags=["shows"])
app.include_router(lists.router, prefix="/lists", tags=["lists"])
app.include_router(profile.router, prefix="/profile", tags=["profile"])
app.include_router(trakt.router, prefix="/trakt", tags=["trakt"])
app.include_router(simkl.router, prefix="/simkl", tags=["simkl"])
app.include_router(mdblist.router, prefix="/mdblist", tags=["mdblist"])
app.include_router(comments.router, prefix="/comments", tags=["comments"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(compat.router, tags=["compat"])

@app.get("/health")
async def health():
    from sqlalchemy import text
    from fastapi.responses import JSONResponse
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "error", "app": "Scrob"})
    return {"status": "ok", "app": "Scrob"}