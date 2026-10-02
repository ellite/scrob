"""Scheduled pushes skip what an earlier push already sent (#421, #422)."""

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core import nuvio
from core import push_state as ps
from core.push_state import MISS, RATING, WATCHED, PushState
from models.collection import Collection, CollectionFile, CollectionSource
from models.connection_push_state import ConnectionPushState
from models.connections import MediaServerConnection
from models.events import WatchEvent
from models.media import Media, MediaType
from models.ratings import Rating
from models.sync import SyncJob, SyncStatus
from routers import sync

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


class PushStateUnitTests(unittest.TestCase):
    NOW = datetime(2026, 9, 21, 12, 0)

    def _state(self, rows=None, **kwargs):
        return PushState(rows, now=self.NOW, **kwargs)

    def test_unchanged_only_when_the_same_value_was_pushed(self):
        state = self._state({(1, RATING): (8.0, self.NOW)})
        self.assertTrue(state.unchanged(1, RATING, 8.0))
        self.assertFalse(state.unchanged(1, RATING, 9.0))
        self.assertFalse(state.unchanged(2, RATING, 8.0))
        self.assertFalse(state.unchanged(1, WATCHED, 8.0))
        self.assertEqual(state.skipped, 1)

    def test_a_none_value_is_never_unchanged(self):
        state = self._state({(1, MISS): (None, self.NOW)})
        self.assertFalse(state.unchanged(1, MISS, None))

    def test_ignoring_state_answers_as_if_nothing_was_pushed(self):
        state = self._state({(1, RATING): (8.0, self.NOW)}, use_state=False)
        self.assertFalse(state.unchanged(1, RATING, 8.0))
        self.assertFalse(state.recent_miss(1))
        self.assertEqual(state.skipped, 0)

    def test_a_resumed_run_trusts_only_what_the_interrupted_run_pushed(self):
        since = self.NOW - timedelta(hours=1)
        state = self._state(
            {
                (1, RATING): (8.0, self.NOW - timedelta(minutes=10)),  # pushed by the interrupted run
                (2, RATING): (8.0, self.NOW - timedelta(days=3)),  # older state, not trusted
            },
            use_state=False,
            resume_since=since,
        )
        self.assertTrue(state.unchanged(1, RATING, 8.0))
        self.assertFalse(state.unchanged(2, RATING, 8.0))

    def test_a_miss_is_trusted_for_a_day(self):
        state = self._state({
            (1, MISS): (None, self.NOW - timedelta(hours=23)),
            (2, MISS): (None, self.NOW - timedelta(hours=25)),
        })
        self.assertTrue(state.recent_miss(1))
        self.assertFalse(state.recent_miss(2))
        self.assertFalse(state.recent_miss(3))

    def test_a_failed_push_is_never_recorded_even_if_a_sibling_worked(self):
        state = self._state()
        state.record(1, RATING, 8.0)
        state.fail(1, RATING)
        state.record(2, RATING, 7.0)
        self.assertEqual(state.pending(), {(2, RATING): 7.0})

    def test_a_miss_is_recorded_regardless(self):
        state = self._state()
        state.record_miss(1)
        state.fail(1, MISS)
        self.assertEqual(state.pending(), {(1, MISS): None})


class _DB(unittest.IsolatedAsyncioTestCase):
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

    async def _new_job(self, connection_id: int, source=CollectionSource.jellyfin) -> int:
        async with self.Session() as db:
            job = SyncJob(
                user_id=1, source=source, status=SyncStatus.pending,
                connection_id=connection_id, job_type="push",
            )
            db.add(job)
            await db.commit()
            return job.id

    async def _job(self, job_id: int) -> SyncJob:
        async with self.Session() as db:
            return (await db.execute(select(SyncJob).where(SyncJob.id == job_id))).scalar_one()

    async def _state_rows(self) -> dict:
        async with self.Session() as db:
            rows = (await db.execute(select(ConnectionPushState))).scalars().all()
            return {(r.media_id, r.item_key): r.value for r in rows}


class PushStatePersistenceTests(_DB):
    async def _connection(self) -> int:
        async with self.Session() as db:
            conn = MediaServerConnection(user_id=1, type="jellyfin", name="J", url="http://jf", token="t")
            db.add(conn)
            await db.flush()
            db.add_all([Media(id=i, tmdb_id=i, media_type=MediaType.movie, title=str(i)) for i in range(1, 6)])
            await db.commit()
            return conn.id

    async def test_save_upserts_and_load_round_trips(self):
        cid = await self._connection()
        plan = ps.PushPlan(ps.MODE_INCREMENTAL, use_state=True)
        async with self.Session() as db:
            state = await PushState.load(db, cid, plan)
            state.record(1, RATING, 8.0)
            state.record_miss(2)
            await state.save(db, cid)
        async with self.Session() as db:
            state = await PushState.load(db, cid, plan)
            self.assertTrue(state.unchanged(1, RATING, 8.0))
            self.assertTrue(state.recent_miss(2))
            state.record(1, RATING, 9.0)
            await state.save(db, cid)
        self.assertEqual(await self._state_rows(), {(1, RATING): 9.0, (2, MISS): None})

    async def test_forget_removes_a_stored_miss(self):
        cid = await self._connection()
        async with self.Session() as db:
            first = PushState()
            first.record_miss(2)
            await first.save(db, cid)
            second = PushState()
            second.forget(2, MISS)
            await second.save(db, cid)
        self.assertEqual(await self._state_rows(), {})

    async def test_save_twice_is_a_no_op(self):
        cid = await self._connection()
        async with self.Session() as db:
            state = PushState()
            state.record(1, RATING, 5.0)
            await state.save(db, cid)
            await state.save(db, cid)
        self.assertEqual(await self._state_rows(), {(1, RATING): 5.0})

    async def test_a_large_batch_is_written_in_chunks(self):
        cid = await self._connection()
        async with self.Session() as db:
            state = PushState()
            for i in range(ps._UPSERT_CHUNK * 2 + 5):
                state.record(1, f"season_rating:{i}", float(i))
            await state.save(db, cid)
        self.assertEqual(len(await self._state_rows()), ps._UPSERT_CHUNK * 2 + 5)

    async def test_deleting_the_connection_removes_its_state(self):
        from sqlalchemy import delete, text

        cid = await self._connection()
        async with self.Session() as db:
            state = PushState()
            state.record(1, RATING, 5.0)
            await state.save(db, cid)
            await db.execute(text("PRAGMA foreign_keys=ON"))
            await db.execute(delete(MediaServerConnection).where(MediaServerConnection.id == cid))
            await db.commit()
        self.assertEqual(await self._state_rows(), {})


class PlanPushTests(_DB):
    async def _job_row(self, *, status, mode=None, created=None, updated=None) -> None:
        async with self.Session() as db:
            job = SyncJob(
                user_id=1, source=CollectionSource.jellyfin, status=status, connection_id=1,
                job_type="push", stats={"mode": mode} if mode else None,
                created_at=created or datetime.utcnow(), updated_at=updated or datetime.utcnow(),
            )
            db.add(job)
            await db.commit()

    async def _plan(self, incremental=True):
        async with self.Session() as db:
            return await ps.plan_push(db, 1, incremental=incremental)

    async def test_a_manual_push_is_always_a_strict_full_push(self):
        await self._job_row(status=SyncStatus.completed, mode="full")
        plan = await self._plan(incremental=False)
        self.assertEqual((plan.mode, plan.use_state, plan.resume_since), ("full", False, None))

    async def test_the_first_scheduled_push_is_full(self):
        plan = await self._plan()
        self.assertEqual((plan.mode, plan.use_state), ("full", False))

    async def test_incremental_after_a_recent_full(self):
        await self._job_row(status=SyncStatus.completed, mode="full")
        plan = await self._plan()
        self.assertEqual((plan.mode, plan.use_state), ("incremental", True))

    async def test_incremental_jobs_do_not_count_as_a_reconcile(self):
        await self._job_row(status=SyncStatus.completed, mode="incremental")
        self.assertEqual((await self._plan()).mode, "full")

    async def test_full_again_once_the_last_reconcile_is_a_week_old(self):
        old = datetime.utcnow() - ps.FULL_RECONCILE_INTERVAL - timedelta(hours=1)
        await self._job_row(status=SyncStatus.completed, mode="full", created=old, updated=old)
        self.assertEqual((await self._plan()).mode, "full")

    async def test_resumes_from_the_first_interrupted_attempt(self):
        first = datetime.utcnow() - timedelta(hours=3)
        second = datetime.utcnow() - timedelta(hours=1)
        await self._job_row(status=SyncStatus.failed, created=first)
        await self._job_row(status=SyncStatus.cancelled, created=second)
        plan = await self._plan()
        self.assertEqual((plan.mode, plan.use_state), ("full", False))
        self.assertEqual(plan.resume_since, first)

    async def test_an_interruption_before_the_last_full_is_not_resumed(self):
        await self._job_row(status=SyncStatus.failed, created=datetime.utcnow() - timedelta(days=9))
        old = datetime.utcnow() - ps.FULL_RECONCILE_INTERVAL - timedelta(hours=1)
        await self._job_row(status=SyncStatus.completed, mode="full", created=old, updated=old)
        self.assertIsNone((await self._plan()).resume_since)


class ServerPushFlowTests(_DB):
    """_run_full_push against Jellyfin/Plex with the server calls stubbed."""

    async def _seed(self, conn_type="jellyfin", *, with_file=True, rating=8.0, watched=False):
        source = CollectionSource(conn_type)
        async with self.Session() as db:
            conn = MediaServerConnection(
                user_id=1, type=conn_type, name=conn_type, url="http://srv", token="tok",
                server_user_id="srv-user", push_ratings=True, push_watched=watched,
            )
            db.add(conn)
            media = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
            db.add(media)
            await db.flush()
            if with_file:
                coll = Collection(user_id=1, media_id=media.id)
                db.add(coll)
                await db.flush()
                db.add(CollectionFile(
                    collection_id=coll.id, connection_id=conn.id, source=source, source_id="srv-item",
                ))
            if rating is not None:
                db.add(Rating(user_id=1, media_id=media.id, rating=rating))
            if watched:
                db.add(WatchEvent(
                    user_id=1, media_id=media.id, watched_at=datetime(2026, 1, 1),
                    completed=True, play_count=1, progress_percent=1.0,
                ))
            await db.commit()
            self.media_id = media.id
            return conn.id

    async def _push(self, conn_id, *, incremental, source=CollectionSource.jellyfin, **patches):
        job_id = await self._new_job(conn_id, source)
        with patch.object(sync, "engine", self.engine):
            await sync._run_full_push(1, conn_id, job_id, incremental=incremental)
        return await self._job(job_id)

    async def test_an_unchanged_rating_is_pushed_once_then_skipped(self):
        cid = await self._seed()
        set_rating = AsyncMock(return_value=True)
        with patch.object(sync.jellyfin, "set_rating", set_rating):
            first = await self._push(cid, incremental=True)
            second = await self._push(cid, incremental=True)
        self.assertEqual(set_rating.await_count, 1)
        self.assertEqual((first.stats["mode"], first.stats["succeeded"], first.stats["skipped"]), ("full", 1, 0))
        self.assertEqual(
            (second.stats["mode"], second.stats["succeeded"], second.stats["skipped"]),
            ("incremental", 0, 1),
        )
        self.assertEqual(second.status, SyncStatus.completed)

    async def test_a_changed_rating_is_pushed_again(self):
        cid = await self._seed()
        set_rating = AsyncMock(return_value=True)
        with patch.object(sync.jellyfin, "set_rating", set_rating):
            await self._push(cid, incremental=True)
            async with self.Session() as db:
                await db.execute(update(Rating).values(rating=9.0))
                await db.commit()
            await self._push(cid, incremental=True)
        self.assertEqual([call.args[4] for call in set_rating.await_args_list], [8.0, 9.0])

    async def test_a_manual_push_always_sends_everything(self):
        cid = await self._seed()
        set_rating = AsyncMock(return_value=True)
        with patch.object(sync.jellyfin, "set_rating", set_rating):
            await self._push(cid, incremental=False)
            second = await self._push(cid, incremental=False)
        self.assertEqual(set_rating.await_count, 2)
        self.assertEqual((second.stats["mode"], second.stats["skipped"]), ("full", 0))

    async def test_a_week_old_reconcile_sends_everything_again(self):
        cid = await self._seed()
        set_rating = AsyncMock(return_value=True)
        with patch.object(sync.jellyfin, "set_rating", set_rating):
            first = await self._push(cid, incremental=True)
            old = datetime.utcnow() - timedelta(days=8)
            async with self.Session() as db:
                await db.execute(update(SyncJob).where(SyncJob.id == first.id).values(updated_at=old))
                await db.commit()
            again = await self._push(cid, incremental=True)
        self.assertEqual(set_rating.await_count, 2)
        self.assertEqual(again.stats["mode"], "full")

    async def test_a_failed_rating_is_retried_by_the_next_scheduled_push(self):
        cid = await self._seed()
        set_rating = AsyncMock(side_effect=[False, True])
        with patch.object(sync.jellyfin, "set_rating", set_rating):
            first = await self._push(cid, incremental=True)
            second = await self._push(cid, incremental=True)
        self.assertEqual(first.stats["failed"], 1)
        self.assertEqual(set_rating.await_count, 2)
        self.assertEqual((second.stats["mode"], second.stats["succeeded"]), ("incremental", 1))

    async def test_a_watched_item_is_marked_once_then_skipped(self):
        cid = await self._seed(rating=None, watched=True)
        mark_watched = AsyncMock(return_value=True)
        watched_state = AsyncMock(return_value={"srv-item": False})
        with patch.object(sync.jellyfin, "mark_watched", mark_watched), \
             patch.object(sync.jellyfin, "get_items_watched_state", watched_state), \
             patch("routers.webhooks.mark_pushed_watched"):
            await self._push(cid, incremental=True)
            second = await self._push(cid, incremental=True)
        self.assertEqual(mark_watched.await_count, 1)
        self.assertEqual(watched_state.await_count, 1)
        self.assertEqual((second.stats["mode"], second.stats["skipped"]), ("incremental", 1))

    async def test_plex_lookup_misses_are_not_repeated_within_a_day(self):
        cid = await self._seed("plex", with_file=False)
        find_movie = AsyncMock(return_value=None)
        with patch.object(sync.plex, "find_movie_by_tmdb_id", find_movie):
            first = await self._push(cid, incremental=True, source=CollectionSource.plex)
            second = await self._push(cid, incremental=True, source=CollectionSource.plex)
            self.assertEqual(find_movie.await_count, 1)
            self.assertEqual(first.stats["failed"], 1)
            # A miss still reads as a failure in the job's numbers, as before.
            self.assertEqual(second.stats["failed"], 1)
            # ...but only for a day: the item may have reached the library since.
            async with self.Session() as db:
                await db.execute(
                    update(ConnectionPushState)
                    .where(ConnectionPushState.item_key == MISS)
                    .values(updated_at=datetime.utcnow() - timedelta(days=2))
                )
                await db.commit()
            await self._push(cid, incremental=True, source=CollectionSource.plex)
        self.assertEqual(find_movie.await_count, 2)

    async def test_a_manual_push_retries_lookup_misses(self):
        cid = await self._seed("plex", with_file=False)
        find_movie = AsyncMock(return_value=None)
        with patch.object(sync.plex, "find_movie_by_tmdb_id", find_movie):
            await self._push(cid, incremental=True, source=CollectionSource.plex)
            await self._push(cid, incremental=False, source=CollectionSource.plex)
        self.assertEqual(find_movie.await_count, 2)

    async def test_a_lookup_that_now_succeeds_clears_the_stored_miss(self):
        cid = await self._seed("plex", with_file=False)
        find_movie = AsyncMock(side_effect=[None, {"ratingKey": "42", "userRating": 8.0}])
        with patch.object(sync.plex, "find_movie_by_tmdb_id", find_movie), \
             patch.object(sync.plex, "set_rating", AsyncMock(return_value=True)):
            await self._push(cid, incremental=False, source=CollectionSource.plex)
            self.assertIn((self.media_id, MISS), await self._state_rows())
            await self._push(cid, incremental=False, source=CollectionSource.plex)
        rows = await self._state_rows()
        self.assertNotIn((self.media_id, MISS), rows)
        self.assertEqual(rows[(self.media_id, RATING)], 8.0)

    async def test_plex_is_not_written_when_it_already_holds_the_rating(self):
        cid = await self._seed("plex")
        set_rating = AsyncMock(return_value=True)
        get_item = AsyncMock(return_value={"ratingKey": "srv-item", "userRating": 8.0})
        with patch.object(sync.plex, "get_item", get_item), patch.object(sync.plex, "set_rating", set_rating):
            job = await self._push(cid, incremental=False, source=CollectionSource.plex)
        set_rating.assert_not_awaited()
        self.assertEqual(job.stats["succeeded"], 1)
        # ...and it is remembered, so the next scheduled push does not even ask.
        self.assertEqual((await self._state_rows())[(self.media_id, RATING)], 8.0)

    async def test_plex_is_written_when_its_rating_differs_or_is_missing(self):
        cid = await self._seed("plex")
        set_rating = AsyncMock(return_value=True)
        get_item = AsyncMock(side_effect=[{"userRating": 6.0}, {"ratingKey": "srv-item"}, None])
        with patch.object(sync.plex, "get_item", get_item), patch.object(sync.plex, "set_rating", set_rating):
            for _ in range(3):
                await self._push(cid, incremental=False, source=CollectionSource.plex)
        self.assertEqual(set_rating.await_count, 3)

    async def test_a_scheduled_push_resumes_an_interrupted_full_push(self):
        cid = await self._seed()
        # An earlier full run was cut short after it had pushed this rating.
        async with self.Session() as db:
            db.add(SyncJob(
                user_id=1, source=CollectionSource.jellyfin, status=SyncStatus.failed,
                connection_id=cid, job_type="push", created_at=datetime.utcnow() - timedelta(hours=1),
            ))
            db.add(ConnectionPushState(
                connection_id=cid, media_id=self.media_id, item_key=RATING, value=8.0,
                updated_at=datetime.utcnow() - timedelta(minutes=30),
            ))
            await db.commit()
        set_rating = AsyncMock(return_value=True)
        with patch.object(sync.jellyfin, "set_rating", set_rating):
            job = await self._push(cid, incremental=True)
        set_rating.assert_not_awaited()
        self.assertEqual((job.stats["mode"], job.stats["skipped"]), ("full", 1))


class NuvioPushFlowTests(_DB):
    """Nuvio's watched snapshot is only re-sent for what changed (#421)."""

    def _entries(self, times: dict[int, int]):
        return [
            (mid, {"content_id": f"tt{mid}", "content_type": "movie", "title": str(mid), "watched_at": ms})
            for mid, ms in times.items()
        ]

    async def _seed(self) -> int:
        async with self.Session() as db:
            conn = MediaServerConnection(
                user_id=1, type="nuvio", name="Nuvio", url="https://api.nuvio.tv", token="refresh",
                server_user_id="1", push_watched=True, push_playback=False, push_collection=False,
            )
            db.add(conn)
            db.add_all([Media(id=i, tmdb_id=i, media_type=MediaType.movie, title=str(i)) for i in (1, 2, 3)])
            await db.commit()
            return conn.id

    async def _push(self, cid, entries, *, incremental, fail_on_call=None):
        pushed: list[list[int]] = []
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/token":
                return httpx.Response(200, json={
                    "access_token": "a", "refresh_token": "rotated", "expires_in": 3600,
                })
            if request.url.path.endswith("/sync_push_watched_items"):
                calls["n"] += 1
                if fail_on_call == calls["n"]:
                    return httpx.Response(429, json={"message": "API rate limit exceeded"})
                pushed.append([int(i["content_id"][2:]) for i in json.loads(request.content)["p_items"]])
                return httpx.Response(204)
            return httpx.Response(404, json={"message": "unexpected request"})

        transport = httpx.MockTransport(handler)
        job_id = await self._new_job(cid, CollectionSource.nuvio)
        with (
            patch.object(sync, "engine", self.engine),
            patch.object(sync, "_build_nuvio_watched_entries", AsyncMock(return_value=entries)),
            patch.object(nuvio, "_PAGE_SIZE", 1),
            patch.object(
                nuvio.httpx, "AsyncClient",
                side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
            ),
        ):
            await sync._run_full_push(1, cid, job_id, incremental=incremental)
        return await self._job(job_id), [m for page in pushed for m in page]

    async def test_only_new_or_changed_watches_are_sent_after_the_first_push(self):
        cid = await self._seed()
        entries = self._entries({1: 1000, 2: 2000, 3: 3000})
        first, sent = await self._push(cid, entries, incremental=True)
        self.assertEqual(sorted(sent), [1, 2, 3])
        self.assertEqual((first.status, first.stats["mode"]), (SyncStatus.completed, "full"))

        second, sent = await self._push(cid, entries, incremental=True)
        self.assertEqual(sent, [])
        self.assertEqual(
            (second.stats["mode"], second.stats["watched"], second.stats["skipped"]),
            ("incremental", 0, 3),
        )

        changed = self._entries({1: 1000, 2: 2500, 3: 3000, })
        third, sent = await self._push(cid, changed, incremental=True)
        self.assertEqual(sent, [2])
        self.assertEqual((third.stats["watched"], third.stats["skipped"]), (1, 2))

    async def test_a_manual_push_resends_everything(self):
        cid = await self._seed()
        entries = self._entries({1: 1000, 2: 2000})
        await self._push(cid, entries, incremental=False)
        _, sent = await self._push(cid, entries, incremental=False)
        self.assertEqual(sorted(sent), [1, 2])

    async def test_pages_that_went_through_before_a_429_are_not_resent(self):
        cid = await self._seed()
        entries = self._entries({1: 1000, 2: 2000, 3: 3000})
        failed, sent = await self._push(cid, entries, incremental=True, fail_on_call=2)
        self.assertEqual(failed.status, SyncStatus.failed)
        self.assertEqual(sent, [1])
        self.assertEqual(await self._state_rows(), {(1, WATCHED): 1000.0})

        # The retry is a full push (nothing reconciled yet) that resumes
        # from where the interrupted one stopped, rather than sending 1 again.
        retry, sent = await self._push(cid, entries, incremental=True)
        self.assertEqual(sorted(sent), [2, 3])
        self.assertEqual((retry.status, retry.stats["mode"], retry.stats["skipped"]), (SyncStatus.completed, "full", 1))


if __name__ == "__main__":
    unittest.main()
