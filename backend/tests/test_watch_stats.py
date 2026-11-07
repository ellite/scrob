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
