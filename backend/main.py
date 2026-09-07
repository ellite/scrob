import asyncio
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse
from dependencies import require_admin
from models.users import User
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from db import engine, Base
import models # noqa: F401
from routers import webhooks, media, history, ratings, sync, shows, auth, lists, oidc, profile, trakt, simkl, mdblist, bingebase, comments, admin, compat, export, yamtrack, calendar, tracker

from core.access_log import install as install_access_log_redaction
install_access_log_redaction()

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from core.limiter import limiter

from sqlalchemy import or_, select, update, delete
from models.sync import SyncJob, SyncStatus
from models.base import CollectionSource
from models.playback_session import PlaybackSession


async def _auto_sync_scheduler():
    from datetime import datetime, timedelta, timezone

    from db import async_sessionmaker
    from models.connections import MediaServerConnection
    from models.users import UserSettings
    from routers.sync import (
        _run_full_push,
        run_emby_sync,
        run_jellyfin_sync,
        run_nuvio_sync,
        run_stremio_sync,
        run_plex_sync,
    )
    from routers.trakt import run_trakt_sync, _run_trakt_push
    from routers.simkl import run_simkl_sync, _run_simkl_push
    from routers.mdblist import run_mdblist_sync, run_mdblist_push

    # Trakt/Simkl/MDBList are single, user-level cloud connections (no
    # MediaServerConnection row, no connection_id) — same due-date logic as
    # the media-connection loop below, just keyed by user_id + source alone.
    cloud_sync_config = [
        {
            "source": CollectionSource.trakt,
            "connected_field": "trakt_access_token",
            "auto_sync_field": "trakt_auto_sync_interval",
            "auto_push_field": "trakt_auto_push_interval",
            "push_flags": ("trakt_push_watched", "trakt_push_ratings", "trakt_push_collection", "trakt_push_dropped"),
            "pull_runner": run_trakt_sync,
            "push_runner": _run_trakt_push,
        },
        {
            "source": CollectionSource.simkl,
            "connected_field": "simkl_access_token",
            "auto_sync_field": "simkl_auto_sync_interval",
            "auto_push_field": "simkl_auto_push_interval",
            "push_flags": ("simkl_push_watched", "simkl_push_ratings"),
            "pull_runner": run_simkl_sync,
            "push_runner": _run_simkl_push,
        },
        {
            "source": CollectionSource.mdblist,
            "connected_field": "mdblist_api_key",
            "auto_sync_field": "mdblist_auto_sync_interval",
            "auto_push_field": "mdblist_auto_push_interval",
            "push_flags": (
                "mdblist_push_watched",
                "mdblist_push_ratings",
                "mdblist_push_watchlist",
                "mdblist_push_collection",
                "mdblist_push_dropped",
            ),
            "pull_runner": run_mdblist_sync,
            "push_runner": run_mdblist_push,
        },
    ]

    check_interval = 300  # seconds between scheduler ticks
    source_map = {
        "jellyfin": CollectionSource.jellyfin,
        "emby": CollectionSource.emby,
        "plex": CollectionSource.plex,
        "nuvio": CollectionSource.nuvio,
        "stremio": CollectionSource.stremio,
    }
    runner_map = {
        "jellyfin": run_jellyfin_sync,
        "emby": run_emby_sync,
        "plex": run_plex_sync,
        "nuvio": run_nuvio_sync,
        "stremio": run_stremio_sync,
    }

    while True:
        await asyncio.sleep(check_interval)
        try:
            async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with async_session() as db:
                result = await db.execute(
                    select(MediaServerConnection).where(
                        or_(
                            MediaServerConnection.auto_sync_interval.isnot(None),
                            MediaServerConnection.auto_push_interval.isnot(None),
                        )
                    )
                )
                connections = result.scalars().all()
                now = datetime.now(timezone.utc).replace(tzinfo=None)

                for conn in connections:
                    source = source_map.get(conn.type)
                    pull_runner = runner_map.get(conn.type)
                    if not source or not pull_runner:
                        continue

                    active_q = await db.execute(
                        select(SyncJob)
                        .where(
                            SyncJob.user_id == conn.user_id,
                            SyncJob.source == source,
                            SyncJob.connection_id == conn.id,
                            SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]),
                        )
                        .limit(1)
                    )
                    if active_q.scalar_one_or_none():
                        continue

                    schedules: list[tuple[str, float, object]] = []
                    if conn.auto_sync_interval is not None:
                        schedules.append(("pull", conn.auto_sync_interval, pull_runner))
                    if conn.auto_push_interval is not None and conn.push_enabled:
                        schedules.append(("push", conn.auto_push_interval, _run_full_push))

                    due: list[tuple[datetime, str, object]] = []
                    for job_type, interval, runner in schedules:
                        last_q = await db.execute(
                            select(SyncJob)
                            .where(
                                SyncJob.user_id == conn.user_id,
                                SyncJob.source == source,
                                SyncJob.connection_id == conn.id,
                                SyncJob.job_type == job_type,
                                SyncJob.status.in_([SyncStatus.completed, SyncStatus.failed]),
                            )
                            .order_by(SyncJob.updated_at.desc())
                            .limit(1)
                        )
                        last_job = last_q.scalar_one_or_none()
                        next_run = (
                            last_job.updated_at + timedelta(hours=interval)
                            if last_job
                            else datetime.min
                        )
                        if next_run <= now:
                            due.append((next_run, job_type, runner))

                    if not due:
                        continue
                    _, job_type, runner = min(due, key=lambda item: item[0])
                    job = SyncJob(
                        user_id=conn.user_id,
                        source=source,
                        status=SyncStatus.pending,
                        connection_id=conn.id,
                        job_type=job_type,
                    )
                    db.add(job)
                    await db.flush()
                    job_id = job.id
                    await db.commit()

                    print(
                        f"Auto-{job_type}: queuing {conn.type} for user {conn.user_id}, "
                        f"connection {conn.id} (job {job_id})"
                    )
                    if job_type == "push":
                        asyncio.create_task(runner(conn.user_id, conn.id, job_id))
                    else:
                        asyncio.create_task(runner(conn.user_id, job_id, 0, 0, conn.id))

                cloud_settings_result = await db.execute(
                    select(UserSettings).where(
                        or_(
                            *[
                                getattr(UserSettings, cfg["auto_sync_field"]).isnot(None)
                                for cfg in cloud_sync_config
                            ],
                            *[
                                getattr(UserSettings, cfg["auto_push_field"]).isnot(None)
                                for cfg in cloud_sync_config
                            ],
                        )
                    )
                )
                cloud_settings_rows = cloud_settings_result.scalars().all()

                for settings_row in cloud_settings_rows:
                    for cfg in cloud_sync_config:
                        source = cfg["source"]
                        auto_sync = getattr(settings_row, cfg["auto_sync_field"])
                        auto_push = getattr(settings_row, cfg["auto_push_field"])
                        if auto_sync is None and auto_push is None:
                            continue
                        if not getattr(settings_row, cfg["connected_field"]):
                            continue

                        active_q = await db.execute(
                            select(SyncJob)
                            .where(
                                SyncJob.user_id == settings_row.user_id,
                                SyncJob.source == source,
                                SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]),
                            )
                            .limit(1)
                        )
                        if active_q.scalar_one_or_none():
                            continue

                        schedules: list[tuple[str, float, object]] = []
                        if auto_sync is not None:
                            schedules.append(("pull", auto_sync, cfg["pull_runner"]))
                        if auto_push is not None and any(
                            getattr(settings_row, flag) for flag in cfg["push_flags"]
                        ):
                            schedules.append(("push", auto_push, cfg["push_runner"]))

                        due: list[tuple[datetime, str, object]] = []
                        for job_type, interval, runner in schedules:
                            last_q = await db.execute(
                                select(SyncJob)
                                .where(
                                    SyncJob.user_id == settings_row.user_id,
                                    SyncJob.source == source,
                                    SyncJob.job_type == job_type,
                                    SyncJob.status.in_([SyncStatus.completed, SyncStatus.failed]),
                                )
                                .order_by(SyncJob.updated_at.desc())
                                .limit(1)
                            )
                            last_job = last_q.scalar_one_or_none()
                            next_run = (
                                last_job.updated_at + timedelta(hours=interval)
                                if last_job
                                else datetime.min
                            )
                            if next_run <= now:
                                due.append((next_run, job_type, runner))

                        if not due:
                            continue
                        _, job_type, runner = min(due, key=lambda item: item[0])
                        job = SyncJob(
                            user_id=settings_row.user_id,
                            source=source,
                            status=SyncStatus.pending,
                            job_type=job_type,
                        )
                        db.add(job)
                        await db.flush()
                        job_id = job.id
                        await db.commit()

                        print(
                            f"Auto-{job_type}: queuing {source.value} for user "
                            f"{settings_row.user_id} (job {job_id})"
                        )
                        asyncio.create_task(runner(settings_row.user_id, job_id))

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


async def _emby_progress_poller():
    """Emby's webhook system has no progress event (only start/pause/unpause/
    stop), so a PlaybackSession opened by an Emby webhook freezes at the last
    event's position until the next one (#240). Jellyfin doesn't need this:
    its Webhook plugin can send PlaybackProgress, which /webhook/jellyfin
    already handles.

    Polls the Emby Sessions API for connections with playback sync enabled and
    refreshes the progress/state of sessions the webhooks already opened.
    Sessions without a matching PlaybackSession row are ignored, so the
    webhook flow stays the source of truth."""
    import logging
    log = logging.getLogger("uvicorn.error")

    try:
        import httpx
        from datetime import datetime
        from db import AsyncSessionLocal
        from models.connections import MediaServerConnection
    except Exception as e:
        log.error(f"Emby progress poller: failed to import dependencies: {e}")
        return

    POLL_INTERVAL = 60
    log.info("Emby progress poller: started")

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            async with AsyncSessionLocal() as db:
                conns = (await db.execute(
                    select(MediaServerConnection).where(
                        MediaServerConnection.type == "emby",
                        MediaServerConnection.sync_playback.is_(True),
                    )
                )).scalars().all()
                for conn in conns:
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            resp = await client.get(
                                f"{conn.url.rstrip('/')}/Sessions",
                                headers={"X-Emby-Token": conn.token},
                            )
                            resp.raise_for_status()
                            sessions = resp.json()
                    except Exception:
                        continue
                    for s in sessions:
                        item = s.get("NowPlayingItem") or {}
                        play_state = s.get("PlayState") or {}
                        runtime = item.get("RunTimeTicks") or 0
                        position = play_state.get("PositionTicks") or 0
                        if not runtime or not position:
                            continue
                        key = f"emby:{conn.user_id}:{s.get('Id')}"
                        row = (await db.execute(
                            select(PlaybackSession).where(PlaybackSession.session_key == key)
                        )).scalar_one_or_none()
                        if not row:
                            continue
                        row.progress_percent = round(position / runtime, 4)
                        row.progress_seconds = int(position / 10_000_000)
                        row.state = "paused" if play_state.get("IsPaused") else "playing"
                        row.updated_at = datetime.utcnow()
                        # Commit per-row via the shared helper (routers/webhooks.py):
                        # a PlaybackStop webhook can delete this same session between
                        # the select above and this write, which SQLAlchemy surfaces
                        # as a StaleDataError. Batching every session into one final
                        # commit would let that one race discard every other Emby
                        # session's progress for this whole poll cycle - tolerating
                        # it per-row keeps the blast radius to just that session.
                        await webhooks._commit_playback_session_update(db)
        except Exception as e:
            log.error(f"Emby progress poller: {e}")


async def _show_metadata_refresher():
    """Keeps every TMDB-backed show's stored metadata (status plus the
    tmdb_data snapshot: seasons, last/next_episode_to_air, refreshed_at) at
    most ~a day old, so Next Up's missing-episode fallback
    (routers/history.py's _next_up_needs_live_fetch) can answer "did a new
    episode appear?" from the database alone instead of fan-out fetching
    TMDB on every cold home-page load (#294, #307, #332).

    This sweep is also what catches revivals: an unexpected renewal (e.g.
    Futurama) flips a locally Ended/Canceled status back within a day. It
    deliberately re-checks each show directly rather than using a
    delta/changes feed - if this process happened to be down during the
    window a changes feed would have caught a revival, that revival is gone
    for good. A direct per-show check has no such window: whether
    yesterday's sweep ran or not, today's checks every stale show from
    scratch.

    Runs shortly after startup (so fresh installs and restarts get their
    snapshots gated quickly), then daily. Shows refreshed less than
    STALE_AFTER ago are skipped, so a restart doesn't re-fetch what
    yesterday's sweep already covered. TVDB-sourced snapshots
    (tmdb_data.source == "tvdb") are never touched - their season layout is
    TVDB-shaped (#335) and their shows have no TMDB identity to fetch.
    """
    import logging
    from datetime import datetime, timedelta, timezone
    log = logging.getLogger("uvicorn.error")

    try:
        from db import AsyncSessionLocal
        from models.show import Show
        from models.global_settings import GlobalSettings
        from models.users import User, UserSettings
        from core import tmdb as tmdb_client
        from routers.media import check_tmdb_key
        from routers.shows import apply_show_metadata
    except Exception as e:
        log.error(f"Show metadata refresher: failed to import dependencies: {e}")
        return

    FINAL_STATUSES = ("Ended", "Canceled")
    STARTUP_DELAY = 2 * 60
    SWEEP_INTERVAL = 24 * 60 * 60
    # Skip shows whose snapshot is younger than this - comfortably less than
    # SWEEP_INTERVAL so clock drift can't make the sweep skip everything, but
    # large enough that a restart right after a sweep re-fetches ~nothing.
    STALE_AFTER = timedelta(hours=20)
    FETCH_CONCURRENCY = 10
    log.info("Show metadata refresher: started")

    def _snapshot_is_fresh(show) -> bool:
        refreshed_at = (show.tmdb_data or {}).get("refreshed_at")
        if not refreshed_at:
            return False
        try:
            refreshed = datetime.fromisoformat(refreshed_at)
        except (TypeError, ValueError):
            return False
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - refreshed < STALE_AFTER

    delay = STARTUP_DELAY
    while True:
        await asyncio.sleep(delay)
        delay = SWEEP_INTERVAL
        try:
            async with AsyncSessionLocal() as db:
                # Show is a shared, instance-wide table with no single
                # "current user" for this sweep to scope to, but a TMDB key
                # isn't tied to whichever account configured it - any valid
                # one fetches the same public show metadata. Global -> an
                # admin's own key -> any user's, so installs that skip the
                # global key still get the sweep instead of it silently
                # never running, while preferring an admin's key over a
                # random member's when both exist.
                gs = (await db.execute(
                    select(GlobalSettings).where(GlobalSettings.id == 1)
                )).scalar_one_or_none()
                api_key = gs.tmdb_api_key if gs else None
                if not check_tmdb_key(api_key):
                    api_key = (await db.execute(
                        select(UserSettings.tmdb_api_key)
                        .join(User, User.id == UserSettings.user_id)
                        .where(UserSettings.tmdb_api_key.isnot(None), User.is_admin.is_(True))
                        .limit(1)
                    )).scalar_one_or_none()
                if not check_tmdb_key(api_key):
                    api_key = (await db.execute(
                        select(UserSettings.tmdb_api_key)
                        .where(UserSettings.tmdb_api_key.isnot(None))
                        .limit(1)
                    )).scalar_one_or_none()
                if not check_tmdb_key(api_key):
                    log.info("Show metadata refresher: no TMDB key configured anywhere, skipping")
                    continue

                all_shows = (await db.execute(
                    select(Show).where(Show.tmdb_id.isnot(None))
                )).scalars().all()
                shows = [
                    s for s in all_shows
                    if (s.tmdb_data or {}).get("source") != "tvdb" and not _snapshot_is_fresh(s)
                ]
                if not shows:
                    continue

                sem = asyncio.Semaphore(FETCH_CONCURRENCY)
                refreshed = 0
                revived = 0

                async def _check(show):
                    nonlocal refreshed, revived
                    was_final = show.status in FINAL_STATUSES
                    async with sem:
                        try:
                            # cache_ttl=None: a stale cached response would
                            # defeat the point of this sweep.
                            data = await tmdb_client.get_show(show.tmdb_id, api_key=api_key, cache_ttl=None)
                        except Exception:
                            return
                    apply_show_metadata(show, data)
                    refreshed += 1
                    if was_final and data.get("status") not in FINAL_STATUSES:
                        revived += 1

                await asyncio.gather(*(_check(s) for s in shows))
                await db.commit()
                log.info(
                    f"Show metadata refresher: refreshed {refreshed}/{len(shows)} stale shows, "
                    f"{revived} revived"
                )
        except Exception as e:
            log.error(f"Show metadata refresher: {e}")


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
                        own_watchlist = await plex_client.get_watchlist(conn.plex_account_token)
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
                            all_friends = await plex_client.get_all_friends(conn.plex_account_token)
                            monitored = set(conn.watchlist_monitored_users or [])
                            friends = [f for f in all_friends if f["watchlist_id"] in monitored] if monitored else []
                            for friend in friends:
                                friend_items = await plex_client.get_friend_watchlist(conn.plex_account_token, friend["watchlist_id"])
                                for fi in friend_items:
                                    plex_id = fi.get("id")
                                    if not plex_id:
                                        continue
                                    cache_key = f"plex:{plex_id}"
                                    if cache_key in synced or cache_key in newly_synced:
                                        continue
                                    enriched = await plex_client.enrich_plex_item(conn.plex_account_token, plex_id)
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
    emby_progress_task = asyncio.create_task(_emby_progress_poller())
    show_metadata_task = asyncio.create_task(_show_metadata_refresher())

    yield

    scheduler_task.cancel()
    watchlist_task.cancel()
    manual_session_task.cancel()
    emby_progress_task.cancel()
    show_metadata_task.cancel()
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
    try:
        await emby_progress_task
    except asyncio.CancelledError:
        pass
    try:
        await show_metadata_task
    except asyncio.CancelledError:
        pass

from core.config import settings

# Rate limiter — keyed by client IP, in-memory storage (suitable for single-instance deploy).
# API docs (docs_url/redoc_url/openapi_url) are disabled here and re-added below behind
# require_admin — the schema reveals the full endpoint surface and exact app version,
# which shouldn't be public on a self-hosted instance that may be internet-facing.
app = FastAPI(title="Scrob", version=settings.app_version, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.get("/openapi.json", include_in_schema=False)
async def get_openapi_schema(_: User = Depends(require_admin)):
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
async def get_docs(_: User = Depends(require_admin)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")


@app.get("/redoc", include_in_schema=False)
async def get_redoc(_: User = Depends(require_admin)):
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")

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
app.include_router(bingebase.router, prefix="/bingebase", tags=["bingebase"])
app.include_router(comments.router, prefix="/comments", tags=["comments"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(export.router, prefix="/export", tags=["export"])
app.include_router(yamtrack.router, prefix="/yamtrack", tags=["yamtrack"])
app.include_router(tracker.router, prefix="/tracker", tags=["tracker"])
app.include_router(calendar.router, prefix="/calendar", tags=["calendar"])
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
