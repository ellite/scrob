"""Finished sync jobs older than a week are pruned (#489)."""

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

from core.job_retention import prune_old_sync_jobs
from models.collection import CollectionSource
from models.sync import SyncJob, SyncStatus


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


class PruneOldSyncJobsTests(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 10, 19, 12, 0)

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

    async def _add(self, status, age_days, connection_id=1, job_type="pull") -> int:
        async with self.Session() as db:
            job = SyncJob(
                user_id=1, source=CollectionSource.plex, status=status, connection_id=connection_id,
                job_type=job_type, updated_at=self.NOW - timedelta(days=age_days),
            )
            db.add(job)
            await db.commit()
            return job.id

    async def _remaining(self) -> set[int]:
        async with self.Session() as db:
            return set((await db.execute(select(SyncJob.id))).scalars().all())

    async def test_prunes_old_finished_jobs_and_keeps_recent_and_active_ones(self):
        newest = await self._add(SyncStatus.completed, 10)  # newest finished job for the connection: kept
        old_completed = await self._add(SyncStatus.completed, 20)
        old_failed = await self._add(SyncStatus.failed, 15)
        old_cancelled = await self._add(SyncStatus.cancelled, 30)
        recent = await self._add(SyncStatus.completed, 2)  # also becomes the newest, so `newest` is now old
        running = await self._add(SyncStatus.running, 40)

        async with self.Session() as db:
            pruned = await prune_old_sync_jobs(db, now=self.NOW)

        self.assertEqual(pruned, 4)
        self.assertEqual(await self._remaining(), {recent, running})
        self.assertNotIn(newest, await self._remaining())
        self.assertTrue({old_completed, old_failed, old_cancelled}.isdisjoint(await self._remaining()))

    async def test_keeps_the_newest_finished_job_per_connection_and_job_type(self):
        pull = await self._add(SyncStatus.completed, 30, connection_id=1, job_type="pull")
        push = await self._add(SyncStatus.completed, 30, connection_id=1, job_type="push")
        other = await self._add(SyncStatus.failed, 30, connection_id=2, job_type="pull")
        older_pull = await self._add(SyncStatus.completed, 40, connection_id=1, job_type="pull")

        async with self.Session() as db:
            pruned = await prune_old_sync_jobs(db, now=self.NOW)

        self.assertEqual(pruned, 1)
        self.assertEqual(await self._remaining(), {pull, push, other})
        self.assertNotIn(older_pull, await self._remaining())


if __name__ == "__main__":
    unittest.main()
