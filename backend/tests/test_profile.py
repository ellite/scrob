import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.profile import get_user_stats


class _Result:
    def __init__(self, scalar=0):
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar

    def all(self):
        return []


class _StatsSession:
    def __init__(self):
        self.statements = []
        self.execute_count = 0

    async def execute(self, statement):
        self.execute_count += 1
        self.statements.append(statement)
        if self.execute_count == 1:
            return _Result(SimpleNamespace(id=7))
        if self.execute_count == 2:
            return _Result(None)
        return _Result(0)


class UnknownWatchDateStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_date_based_stats_exclude_unknown_watch_dates(self) -> None:
        db = _StatsSession()

        stats = await get_user_stats(
            7,
            db=db,
            current_user=SimpleNamespace(id=7, role="user"),
        )

        statements = [str(statement).lower() for statement in db.statements]
        dated_queries = [
            statement
            for statement in statements
            if "to_char(watch_events.watched_at" in statement
            or "extract(dow from watch_events.watched_at" in statement
        ]
        self.assertEqual(len(dated_queries), 4)
        self.assertTrue(all(
            "watch_events.watched_at is not null" in statement
            for statement in dated_queries
        ))
        self.assertEqual(stats["watch_activity"], [])
        self.assertEqual(
            stats["weekday_activity"],
            [
                {"day": day, "avg": 0.0}
                for day in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
