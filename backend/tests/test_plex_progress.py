"""Scrob → Plex playback progress push (core/plex_progress.py)."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core import plex, plex_progress
from core.push_state import MISS, PROGRESS, PushState
from models.connections import MediaServerConnection
from routers import sync


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


_REAL_ASYNC_CLIENT = httpx.AsyncClient

NOW = datetime(2026, 10, 29, 13, 0, 0)


def _epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class SetProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_the_position_through_the_progress_endpoint(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            captured["token"] = request.headers.get("X-Plex-Token")
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        with patch.object(
            plex.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            ok = await plex.set_progress("http://plex.local/", "tok", "42", 1_234_000)

        self.assertTrue(ok)
        self.assertEqual(captured["path"], "/:/progress")
        self.assertEqual(captured["params"], {
            "key": "42",
            "identifier": "com.plexapp.plugins.library",
            "time": "1234000",
            "state": "stopped",
        })
        self.assertEqual(captured["token"], "tok")

    async def test_reports_a_server_error_as_failure(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(500))
        with patch.object(
            plex.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            self.assertFalse(await plex.set_progress("http://plex.local", "tok", "42", 1000))


class PlexAlreadyMatchesTests(unittest.TestCase):
    def _progress(self, seconds=600, updated_at=NOW):
        return SimpleNamespace(progress_seconds=seconds, updated_at=updated_at)

    def test_same_position_needs_no_write(self):
        self.assertTrue(plex_progress.plex_already_matches({"viewOffset": 604_000}, self._progress()))

    def test_different_position_needs_a_write(self):
        self.assertFalse(plex_progress.plex_already_matches({"viewOffset": 120_000}, self._progress()))

    def test_item_without_a_position_needs_a_write(self):
        self.assertFalse(plex_progress.plex_already_matches({}, self._progress()))

    def test_newer_plex_playback_wins(self):
        item = {"viewOffset": 120_000, "lastViewedAt": _epoch(NOW + timedelta(minutes=10))}
        self.assertTrue(plex_progress.plex_already_matches(item, self._progress()))

    def test_older_plex_playback_is_overwritten(self):
        item = {"viewOffset": 120_000, "lastViewedAt": _epoch(NOW - timedelta(days=2))}
        self.assertFalse(plex_progress.plex_already_matches(item, self._progress()))

    def test_clock_skew_within_the_slack_is_not_newer(self):
        item = {"viewOffset": 120_000, "lastViewedAt": _epoch(NOW + timedelta(seconds=30))}
        self.assertFalse(plex_progress.plex_already_matches(item, self._progress()))


class _ProgressDB(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import models  # noqa: F401 - registers every table on Base.metadata
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

    async def _seed(self, *, push_playback=True, rating_key="rk-1", seconds=600, push_watched=False):
        """One Plex connection and one movie with a resume point, optionally
        already seen on that server under ``rating_key``."""
        from models.collection import Collection, CollectionFile, CollectionSource
        from models.media import Media, MediaType
        from models.playback_progress import PlaybackProgress
        from models.sync import SyncJob, SyncStatus

        async with self.Session() as db:
            conn = MediaServerConnection(
                user_id=1, type="plex", name="Plex", url="http://plex", token="tok",
                push_playback=push_playback, push_watched=push_watched,
            )
            db.add(conn)
            await db.flush()
            job = SyncJob(
                user_id=1, source=CollectionSource.plex, status=SyncStatus.pending,
                connection_id=conn.id, job_type="push",
            )
            db.add(job)
            media = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix", runtime=136)
            db.add(media)
            await db.flush()
            if rating_key:
                coll = Collection(user_id=1, media_id=media.id)
                db.add(coll)
                await db.flush()
                db.add(CollectionFile(
                    collection_id=coll.id, connection_id=conn.id,
                    source=CollectionSource.plex, source_id=rating_key,
                ))
            db.add(PlaybackProgress(
                user_id=1, media_id=media.id, progress_percent=0.07,
                progress_seconds=seconds, updated_at=NOW,
            ))
            await db.commit()
            self.conn_id, self.job_id, self.media_id = conn.id, job.id, media.id

    async def _conn(self, db):
        return (await db.execute(
            select(MediaServerConnection).where(MediaServerConnection.id == self.conn_id)
        )).scalar_one()


class PushProgressToPlexTests(_ProgressDB):
    async def test_pushes_a_known_item_and_records_it(self):
        await self._seed()
        set_progress = AsyncMock(return_value=True)
        with patch.object(plex, "get_item", AsyncMock(return_value={"ratingKey": "rk-1", "viewOffset": 0})), \
             patch.object(plex, "set_progress", set_progress):
            async with self.Session() as db:
                state = PushState(use_state=False)
                result = await plex_progress.push_progress_to_plex(db, await self._conn(db), 1, state)

        self.assertEqual((result.pushed, result.failed, result.unchanged, result.missing), (1, 0, 0, 0))
        self.assertEqual(set_progress.await_args.args, ("http://plex", "tok", "rk-1", 600_000))
        self.assertEqual(state.pending(), {(self.media_id, PROGRESS): 600.0})

    async def test_matching_position_is_not_rewritten(self):
        await self._seed()
        set_progress = AsyncMock(return_value=True)
        with patch.object(plex, "get_item", AsyncMock(return_value={"ratingKey": "rk-1", "viewOffset": 601_500})), \
             patch.object(plex, "set_progress", set_progress):
            async with self.Session() as db:
                state = PushState(use_state=False)
                result = await plex_progress.push_progress_to_plex(db, await self._conn(db), 1, state)

        set_progress.assert_not_awaited()
        self.assertEqual(result.unchanged, 1)
        # Plex already holds it, so the next incremental push can skip it.
        self.assertEqual(state.pending(), {(self.media_id, PROGRESS): 600.0})

    async def test_incremental_push_skips_a_position_already_sent(self):
        await self._seed()
        get_item = AsyncMock()
        state = PushState({(self.media_id, PROGRESS): (600.0, NOW)}, use_state=True)
        with patch.object(plex, "get_item", get_item), patch.object(plex, "set_progress", AsyncMock()):
            async with self.Session() as db:
                result = await plex_progress.push_progress_to_plex(db, await self._conn(db), 1, state)

        get_item.assert_not_awaited()
        self.assertEqual(result.attempted, 0)
        self.assertEqual(state.skipped, 1)

    async def test_incremental_push_resends_a_moved_position(self):
        await self._seed(seconds=900)
        set_progress = AsyncMock(return_value=True)
        state = PushState({(self.media_id, PROGRESS): (600.0, NOW)}, use_state=True)
        with patch.object(plex, "get_item", AsyncMock(return_value={"ratingKey": "rk-1", "viewOffset": 600_000})), \
             patch.object(plex, "set_progress", set_progress):
            async with self.Session() as db:
                result = await plex_progress.push_progress_to_plex(db, await self._conn(db), 1, state)

        self.assertEqual(result.pushed, 1)
        self.assertEqual(set_progress.await_args.args[3], 900_000)

    async def test_unknown_item_is_looked_up_in_the_libraries(self):
        await self._seed(rating_key=None)
        found = {"ratingKey": 77, "viewOffset": 0}
        set_progress = AsyncMock(return_value=True)
        with patch.object(plex, "find_movie_by_tmdb_id", AsyncMock(return_value=found)) as find, \
             patch.object(plex, "get_item", AsyncMock()) as get_item, \
             patch.object(plex, "set_progress", set_progress):
            async with self.Session() as db:
                state = PushState(use_state=False)
                result = await plex_progress.push_progress_to_plex(db, await self._conn(db), 1, state)

        find.assert_awaited_once_with("http://plex", "tok", 603)
        # The lookup already returned the item; no second fetch.
        get_item.assert_not_awaited()
        self.assertEqual(result.pushed, 1)
        self.assertEqual(set_progress.await_args.args[2], "77")

    async def test_item_missing_from_plex_is_recorded_as_a_miss(self):
        await self._seed(rating_key=None)
        with patch.object(plex, "find_movie_by_tmdb_id", AsyncMock(return_value=None)), \
             patch.object(plex, "set_progress", AsyncMock()) as set_progress:
            async with self.Session() as db:
                state = PushState(use_state=False)
                result = await plex_progress.push_progress_to_plex(db, await self._conn(db), 1, state)

        set_progress.assert_not_awaited()
        self.assertEqual(result.missing, 1)
        self.assertEqual(state.pending(), {(self.media_id, MISS): None})

    async def test_no_lookup_when_lookups_are_off(self):
        await self._seed(rating_key=None)
        with patch.object(plex, "find_movie_by_tmdb_id", AsyncMock()) as find, \
             patch.object(plex, "set_progress", AsyncMock()) as set_progress:
            async with self.Session() as db:
                state = PushState(use_state=False)
                result = await plex_progress.push_progress_to_plex(
                    db, await self._conn(db), 1, state, allow_lookup=False,
                )

        find.assert_not_awaited()
        set_progress.assert_not_awaited()
        self.assertEqual(result.missing, 1)
        self.assertEqual(state.pending(), {})

    async def test_failed_write_is_not_recorded_as_pushed(self):
        await self._seed()
        with patch.object(plex, "get_item", AsyncMock(return_value={"ratingKey": "rk-1", "viewOffset": 0})), \
             patch.object(plex, "set_progress", AsyncMock(return_value=False)):
            async with self.Session() as db:
                state = PushState(use_state=False)
                result = await plex_progress.push_progress_to_plex(db, await self._conn(db), 1, state)

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_media_ids, [self.media_id])
        self.assertEqual(state.pending(), {})

    async def test_stale_rating_key_counts_as_missing(self):
        await self._seed()
        with patch.object(plex, "get_item", AsyncMock(return_value=None)), \
             patch.object(plex, "set_progress", AsyncMock()) as set_progress:
            async with self.Session() as db:
                state = PushState(use_state=False)
                result = await plex_progress.push_progress_to_plex(db, await self._conn(db), 1, state)

        set_progress.assert_not_awaited()
        self.assertEqual(result.missing, 1)
        self.assertEqual(state.pending(), {})

    async def test_connection_without_the_flag_pushes_nothing(self):
        await self._seed(push_playback=False)
        with patch.object(plex, "set_progress", AsyncMock()) as set_progress:
            async with self.Session() as db:
                result = await plex_progress.push_progress_to_plex(
                    db, await self._conn(db), 1, PushState(use_state=False),
                )

        set_progress.assert_not_awaited()
        self.assertEqual(result.attempted, 0)


class FullPushProgressTests(_ProgressDB):
    async def _job(self):
        from models.sync import SyncJob

        async with self.Session() as db:
            return (await db.execute(select(SyncJob).where(SyncJob.id == self.job_id))).scalar_one()

    async def test_progress_only_connection_still_pushes(self):
        # No watched/rating push enabled: the job used to stop at "nothing to
        # push" before reaching anything else.
        await self._seed()
        set_progress = AsyncMock(return_value=True)
        with patch.object(sync, "engine", self.engine), \
             patch.object(plex, "get_item", AsyncMock(return_value={"ratingKey": "rk-1", "viewOffset": 0})), \
             patch.object(plex, "set_progress", set_progress):
            await sync._run_full_push(1, self.conn_id, self.job_id)

        set_progress.assert_awaited_once()
        job = await self._job()
        self.assertEqual(job.status.value, "completed")
        self.assertEqual(job.stats["progress"], 1)
        self.assertEqual(job.stats["mode"], "full")

        from models.connection_push_state import ConnectionPushState

        async with self.Session() as db:
            rows = (await db.execute(
                select(ConnectionPushState.item_key, ConnectionPushState.value)
                .where(ConnectionPushState.connection_id == self.conn_id)
            )).all()
        self.assertEqual(rows, [(PROGRESS, 600.0)])


class PushProgressAfterStopTests(_ProgressDB):
    async def test_pushes_a_known_item_in_its_own_session(self):
        await self._seed()
        set_progress = AsyncMock(return_value=True)
        with patch.object(plex_progress, "engine", self.engine), \
             patch.object(plex, "get_item", AsyncMock(return_value={"ratingKey": "rk-1", "viewOffset": 0})), \
             patch.object(plex, "set_progress", set_progress):
            pushed = await plex_progress.push_progress_after_stop(1, [self.media_id])

        self.assertEqual(pushed, 1)
        set_progress.assert_awaited_once()

    async def test_never_searches_the_libraries(self):
        await self._seed(rating_key=None)
        with patch.object(plex_progress, "engine", self.engine), \
             patch.object(plex, "find_movie_by_tmdb_id", AsyncMock()) as find, \
             patch.object(plex, "set_progress", AsyncMock()) as set_progress:
            pushed = await plex_progress.push_progress_after_stop(1, [self.media_id])

        self.assertEqual(pushed, 0)
        find.assert_not_awaited()
        set_progress.assert_not_awaited()

    async def test_a_plex_failure_is_swallowed(self):
        await self._seed()
        with patch.object(plex_progress, "engine", self.engine), \
             patch.object(plex, "get_item", AsyncMock(side_effect=RuntimeError("boom"))):
            pushed = await plex_progress.push_progress_after_stop(1, [self.media_id])

        self.assertEqual(pushed, 0)


class PushStopProgressGateTests(_ProgressDB):
    """routers.webhooks._push_stop_progress only starts a background push for
    users with a Plex connection that pushes playback progress."""

    async def test_schedules_with_a_target(self):
        from routers import webhooks

        await self._seed()
        with patch.object(webhooks, "schedule_progress_push_after_stop") as schedule:
            async with self.Session() as db:
                await webhooks._push_stop_progress(db, 1, [self.media_id])
        schedule.assert_called_once_with(1, [self.media_id])

    async def test_does_nothing_without_a_target(self):
        from routers import webhooks

        await self._seed(push_playback=False)
        with patch.object(webhooks, "schedule_progress_push_after_stop") as schedule:
            async with self.Session() as db:
                await webhooks._push_stop_progress(db, 1, [self.media_id])
        schedule.assert_not_called()


class ConnectionPushPlaybackFlagTests(unittest.IsolatedAsyncioTestCase):
    """Plex connections may now keep push_playback; Jellyfin and Emby still
    can't."""

    async def _patch(self, conn, **fields):
        import schemas
        from routers import auth as auth_router

        class _DB:
            commit = AsyncMock()
            refresh = AsyncMock()

            async def execute(self, stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: conn)

        body = schemas.MediaServerConnectionUpdate(**fields)
        return await auth_router.update_connection(
            connection_id=1, body=body, db=_DB(), current_user=SimpleNamespace(id=1),
        )

    def _conn(self, kind):
        return MediaServerConnection(
            id=1, user_id=1, type=kind, name=kind, url="http://s", token="t", push_playback=False,
        )

    async def test_plex_can_turn_it_on(self):
        conn = self._conn("plex")
        await self._patch(conn, push_playback=True)
        self.assertTrue(conn.push_playback)

    async def test_plex_keeps_it_when_another_flag_changes(self):
        conn = self._conn("plex")
        conn.push_playback = True
        await self._patch(conn, push_watched=True)
        self.assertTrue(conn.push_playback)

    async def test_jellyfin_still_cannot(self):
        conn = self._conn("jellyfin")
        await self._patch(conn, push_playback=True)
        self.assertFalse(conn.push_playback)


if __name__ == "__main__":
    unittest.main()
