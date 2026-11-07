import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.watch_stats import WatchStats, get_watch_stats


def db_returning(row):
    result = MagicMock()
    result.one.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class GetWatchStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_last_watched_at_as_iso_string(self):
        # Arrange
        db = db_returning((3, 90, datetime(2026, 10, 12, 21, 5)))

        # Act
        stats = await get_watch_stats(db, 1, show_id=7)

        # Assert
        self.assertEqual(stats, WatchStats(3, 90, "2026-10-12T21:05:00"))

    async def test_last_watched_at_is_none_without_dated_completed_plays(self):
        # Arrange
        db = db_returning((2, 0, None))

        # Act
        stats = await get_watch_stats(db, 1, tmdb_id=5, media_type="movie")

        # Assert
        self.assertIsNone(stats.last_watched_at)

    async def test_empty_media_ids_skips_the_query(self):
        # Arrange
        db = db_returning(None)

        # Act
        stats = await get_watch_stats(db, 1, media_ids=[])

        # Assert
        self.assertEqual(stats, WatchStats(0, 0, None))
        db.execute.assert_not_called()

    def test_query_filters_last_watched_to_completed_plays(self):
        # Arrange
        from sqlalchemy.dialects import postgresql
        import core.watch_stats as ws
        captured = {}

        async def capture(query):
            captured["sql"] = str(query.compile(dialect=postgresql.dialect()))
            result = MagicMock()
            result.one.return_value = (0, 0, None)
            return result

        db = MagicMock()
        db.execute = capture

        # Act
        import asyncio
        asyncio.run(ws.get_watch_stats(db, 1, show_id=7))

        # Assert
        self.assertIn("max(CASE WHEN (watch_events.completed = true) THEN watch_events.watched_at END)", captured["sql"])


class WatchStatsOnRealRowsTests(unittest.IsolatedAsyncioTestCase):
    """The counting runs in SQL, so check it against real rows."""

    async def asyncSetUp(self):
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.ext.compiler import compiles
        from sqlalchemy.pool import StaticPool
        from models.base import Base

        compiles(JSONB, "sqlite")(lambda type_, compiler, **kw: "JSON")
        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

    async def test_in_progress_events_are_not_plays(self):
        from models.events import WatchEvent
        from models.media import Media, MediaType

        async with self.Session() as db:
            db.add_all([
                Media(id=1, tmdb_id=11, media_type=MediaType.episode, title="E1", show_id=7, season_number=1, episode_number=1, runtime=40),
                Media(id=2, tmdb_id=12, media_type=MediaType.episode, title="E2", show_id=7, season_number=1, episode_number=2, runtime=40),
            ])
            await db.commit()
            db.add_all([
                WatchEvent(user_id=1, media_id=1, watched_at=datetime(2026, 1, 1), completed=True),
                WatchEvent(user_id=1, media_id=1, watched_at=datetime(2026, 2, 1), completed=True),
                WatchEvent(user_id=1, media_id=2, watched_at=datetime(2026, 3, 1), completed=False),
                WatchEvent(user_id=2, media_id=1, watched_at=datetime(2026, 4, 1), completed=True),
            ])
            await db.commit()

            stats = await get_watch_stats(db, 1, show_id=7)

        # Two completed plays of E1 only: the in-progress E2 and the other user's play do not count.
        self.assertEqual(stats, WatchStats(2, 80, "2026-02-01T00:00:00"))

