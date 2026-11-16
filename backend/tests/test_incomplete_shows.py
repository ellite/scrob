import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.dialects import postgresql

from routers import shows as shows_router


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _row(show_id, collected, episodes=10, seasons=True, status="Ended", next_ep=None):
    """One result row of the grouped query: show id, the cached tmdb_data keys
    total_aired_episodes reads, and the collected-episode count."""
    return SimpleNamespace(
        id=show_id,
        status=status,
        seasons=[{"season_number": 1, "episode_count": episodes, "air_date": "2000-01-01"}] if seasons else None,
        last_episode_to_air=None,
        next_episode_to_air=next_ep,
        collected=collected,
    )


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class IncompleteShowIdsTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_only_partly_collected_shows(self):
        rows = [
            _row(1, collected=10),                 # complete
            _row(2, collected=4),                  # incomplete
            _row(3, collected=12),                 # more than the cache says aired: not incomplete
            _row(4, collected=3, seasons=False),   # no season data: unknown, not incomplete
        ]
        db = SimpleNamespace(execute=AsyncMock(return_value=_Rows(rows)))
        self.assertEqual(await shows_router._incomplete_show_ids(db, 7), [2])

    async def test_directly_collected_show_with_no_episode_rows_is_incomplete(self):
        db = SimpleNamespace(execute=AsyncMock(return_value=_Rows([_row(7, collected=None)])))
        self.assertEqual(await shows_router._incomplete_show_ids(db, 7), [7])

    async def test_airing_show_counts_only_aired_episodes(self):
        # 10 episodes announced, episode 6 is next: 5 aired.
        next_ep = {"season_number": 1, "episode_number": 6, "air_date": "2999-01-01"}
        rows = [
            _row(5, collected=5, status="Returning Series", next_ep=next_ep),
            _row(6, collected=3, status="Returning Series", next_ep=next_ep),
        ]
        db = SimpleNamespace(execute=AsyncMock(return_value=_Rows(rows)))
        self.assertEqual(await shows_router._incomplete_show_ids(db, 7), [6])

    async def test_one_grouped_query_reads_only_season_keys(self):
        db = SimpleNamespace(execute=AsyncMock(return_value=_Rows([])))
        await shows_router._incomplete_show_ids(db, 7)
        self.assertEqual(db.execute.await_count, 1)
        sql = _sql(db.execute.await_args.args[0])
        self.assertIn("collections.user_id", sql)
        self.assertIn("GROUP BY", sql)
        self.assertIn("AS seasons", sql)
        self.assertIn("AS next_episode_to_air", sql)

    async def test_500_shows_one_query(self):
        rows = [_row(i, collected=5 if i % 2 else 10) for i in range(1, 501)]
        db = SimpleNamespace(execute=AsyncMock(return_value=_Rows(rows)))
        ids = await shows_router._incomplete_show_ids(db, 7)
        self.assertEqual(len(ids), 250)
        self.assertEqual(db.execute.await_count, 1)  # no per-show queries


class _ListResult:
    def scalar_one(self):
        return 0

    def scalars(self):
        return self

    def all(self):
        return []


class ListShowsIncompleteTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, ids, **kw):
        args = dict(
            sort="title", page=1, page_size=30, genre=[], year=[], status=[], watched=[],
            in_list=None, my_rating=[], incomplete=False,
        )
        args.update(kw)
        db = SimpleNamespace(execute=AsyncMock(return_value=_ListResult()))
        helper = AsyncMock(return_value=ids)
        with patch.object(shows_router, "enrich_with_state", AsyncMock()), \
             patch.object(shows_router, "get_user_metadata_language", AsyncMock(return_value=None)), \
             patch.object(shows_router, "_incomplete_show_ids", helper):
            await shows_router.list_shows(db=db, current_user=SimpleNamespace(id=7), **args)
        return helper, [_sql(c.args[0]) for c in db.execute.await_args_list]

    async def test_incomplete_restricts_count_and_page(self):
        helper, sqls = await self._call([5, 9], incomplete=True)
        helper.assert_awaited_once()
        self.assertEqual(len(sqls), 2)
        for sql in sqls:
            self.assertIn("shows.id IN (__[POSTCOMPILE_", sql)

    async def test_off_by_default_never_runs_the_helper(self):
        helper, sqls = await self._call([5])
        helper.assert_not_awaited()
        for sql in sqls:
            self.assertNotIn("POSTCOMPILE", sql)


if __name__ == "__main__":
    unittest.main()
