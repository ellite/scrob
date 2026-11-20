"""#584: a Nuvio/Stremio pull imports an episode's mid-playback position and,
later, its completed watch. The position must not outlive the completion in
Continue Watching, while a newer partial rewatch must survive."""
import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from models.base import Base, MediaType
from models.events import WatchEvent
from models.media import Media
from models.playback_progress import PlaybackProgress
from routers.sync import _prune_obsolete_progress

USER, OTHER = 1, 2
DONE = datetime(2026, 11, 1, 20, 0, 0)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


class PruneObsoleteProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import models  # noqa: F401 - registers every table

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)
        async with self.Session() as db:
            for i in (1, 2, 3):
                db.add(Media(id=i, media_type=MediaType.episode, title=f"Ep {i}"))
            await db.commit()

    async def _seed(self, *, progress, watches):
        async with self.Session() as db:
            for user_id, media_id, updated_at in progress:
                db.add(PlaybackProgress(
                    user_id=user_id, media_id=media_id, progress_percent=0.5,
                    progress_seconds=60, updated_at=updated_at,
                ))
            for user_id, media_id, watched_at, completed in watches:
                db.add(WatchEvent(
                    user_id=user_id, media_id=media_id, watched_at=watched_at,
                    completed=completed, play_count=1, progress_percent=1.0, source="nuvio",
                ))
            await db.commit()

    async def _remaining(self, user_id=USER):
        async with self.Session() as db:
            result = await db.execute(select(PlaybackProgress.media_id).where(PlaybackProgress.user_id == user_id))
            return sorted(r[0] for r in result.all())

    async def test_pause_resume_finish_clears_the_old_position(self):
        # paused at 20:00 minus an hour, finished at 20:00
        await self._seed(
            progress=[(USER, 1, DONE - timedelta(hours=1))],
            watches=[(USER, 1, DONE, True)],
        )
        async with self.Session() as db:
            self.assertEqual(await _prune_obsolete_progress(db, USER), 1)
        self.assertEqual(await self._remaining(), [])

    async def test_newer_partial_rewatch_is_kept(self):
        await self._seed(
            progress=[(USER, 1, DONE + timedelta(days=3))],
            watches=[(USER, 1, DONE, True)],
        )
        async with self.Session() as db:
            self.assertEqual(await _prune_obsolete_progress(db, USER), 0)
        self.assertEqual(await self._remaining(), [1])

    async def test_position_reported_just_as_playback_finished_is_cleared(self):
        await self._seed(
            progress=[(USER, 1, DONE + timedelta(minutes=2))],
            watches=[(USER, 1, DONE, True)],
        )
        async with self.Session() as db:
            self.assertEqual(await _prune_obsolete_progress(db, USER), 1)

    async def test_position_without_any_completed_watch_is_kept(self):
        await self._seed(
            progress=[(USER, 1, DONE)],
            watches=[(USER, 1, DONE + timedelta(hours=1), False)],  # not completed
        )
        async with self.Session() as db:
            self.assertEqual(await _prune_obsolete_progress(db, USER), 0)
        self.assertEqual(await self._remaining(), [1])

    async def test_watch_with_unknown_date_never_clears_progress(self):
        await self._seed(progress=[(USER, 1, DONE)], watches=[(USER, 1, None, True)])
        async with self.Session() as db:
            self.assertEqual(await _prune_obsolete_progress(db, USER), 0)

    async def test_other_users_watches_and_progress_are_untouched(self):
        await self._seed(
            progress=[(USER, 1, DONE - timedelta(hours=1)), (OTHER, 1, DONE - timedelta(hours=1))],
            watches=[(OTHER, 2, DONE, True), (USER, 3, DONE, True)],
        )
        async with self.Session() as db:
            self.assertEqual(await _prune_obsolete_progress(db, USER), 0)
        self.assertEqual(await self._remaining(USER), [1])
        self.assertEqual(await self._remaining(OTHER), [1])

    async def test_only_the_finished_episode_is_cleared(self):
        await self._seed(
            progress=[(USER, 1, DONE - timedelta(hours=1)), (USER, 2, DONE - timedelta(hours=1))],
            watches=[(USER, 1, DONE, True)],
        )
        async with self.Session() as db:
            await _prune_obsolete_progress(db, USER)
        self.assertEqual(await self._remaining(), [2])

    async def test_no_progress_rows_is_a_noop(self):
        async with self.Session() as db:
            self.assertEqual(await _prune_obsolete_progress(db, USER), 0)
