import asyncio
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import external_ratings as er

MDBLIST_ITEM = {
    "ratings": [
        {"source": "imdb", "value": 8.3, "votes": 1052000},
        {"source": "tomatoes", "value": 97, "votes": 89, "url": "/m/good_will_hunting"},
        {"source": "popcorn", "value": 94, "votes": 15833, "url": "/m/good_will_hunting"},
        {"source": "metacritic", "value": None},
        {"source": "letterboxd", "value": 4.1},
    ]
}


class ParseTests(unittest.TestCase):
    def test_maps_sources_and_skips_missing_values(self) -> None:
        out = er.parse(MDBLIST_ITEM)
        self.assertEqual(out["imdb"], {"value": 8.3, "votes": 1052000, "url": None})
        self.assertEqual(out["popcornmeter"]["value"], 94.0)
        self.assertEqual(out["tomatometer"]["url"], "https://www.rottentomatoes.com/m/good_will_hunting")
        self.assertEqual(out["letterboxd"]["votes"], None)
        self.assertNotIn("metacritic", out)
        self.assertNotIn("trakt", out)

    def test_popcornmeter_accepts_legacy_source_name(self) -> None:
        out = er.parse({"ratings": [{"source": "tomatoesaudience", "value": "71"}]})
        self.assertEqual(out["popcornmeter"]["value"], 71.0)

    def test_garbage_is_empty(self) -> None:
        self.assertEqual(er.parse(None), {})
        self.assertEqual(er.parse({"ratings": [{"source": "imdb", "value": "n/a"}]}), {})


def _row(age: timedelta, ratings: dict) -> SimpleNamespace:
    return SimpleNamespace(ratings=ratings, fetched_at=datetime.utcnow() - age)


class GetRatingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        er._paused_until = 0.0
        er._inflight.clear()

    async def test_fresh_row_is_served_without_fetching(self) -> None:
        fetch = AsyncMock()
        with patch.object(er, "_load", AsyncMock(return_value=_row(timedelta(hours=1), {"imdb": {"value": 7.0}}))), \
             patch.object(er, "_fetch_and_store", fetch):
            out = await er.get_ratings(None, "key", "movie", 1)
        self.assertEqual(out["imdb"]["value"], 7.0)
        fetch.assert_not_called()

    async def test_stale_row_is_refreshed(self) -> None:
        with patch.object(er, "_load", AsyncMock(return_value=_row(timedelta(days=2), {"imdb": {"value": 7.0}}))), \
             patch.object(er, "_fetch_and_store", AsyncMock(return_value={"imdb": {"value": 7.4}})):
            out = await er.get_ratings(None, "key", "movie", 1)
        self.assertEqual(out["imdb"]["value"], 7.4)

    async def test_slow_mdblist_returns_old_values_after_timeout(self) -> None:
        async def slow(*_args):
            await asyncio.sleep(1)
            return {"imdb": {"value": 9.9}}
        with patch.object(er, "FETCH_WAIT_SECONDS", 0.05), \
             patch.object(er, "_load", AsyncMock(return_value=_row(timedelta(days=2), {"imdb": {"value": 7.0}}))), \
             patch.object(er, "_fetch_and_store", side_effect=slow):
            out = await er.get_ratings(None, "key", "movie", 1)
            self.assertEqual(out["imdb"]["value"], 7.0)
            await asyncio.sleep(1.1)  # background fetch finishes and is dropped from _inflight
        self.assertEqual(er._inflight, {})

    async def test_concurrent_requests_share_one_fetch(self) -> None:
        calls = 0

        async def fetch(*_args):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return {"popcornmeter": {"value": 94.0}}
        with patch.object(er, "_load", AsyncMock(return_value=None)), \
             patch.object(er, "_fetch_and_store", side_effect=fetch):
            results = await asyncio.gather(*[er.get_ratings(None, "key", "show", 5) for _ in range(5)])
        self.assertEqual(calls, 1)
        self.assertTrue(all(r["popcornmeter"]["value"] == 94.0 for r in results))

    async def test_no_key_or_paused_never_fetches(self) -> None:
        fetch = AsyncMock()
        with patch.object(er, "_load", AsyncMock(return_value=None)), patch.object(er, "_fetch_and_store", fetch):
            self.assertEqual(await er.get_ratings(None, None, "movie", 1), {})
            er._paused_until = float("inf")
            self.assertEqual(await er.get_ratings(None, "key", "movie", 1), {})
        fetch.assert_not_called()


class CachedMapTests(unittest.IsolatedAsyncioTestCase):
    async def test_splits_values_stale_and_missing(self) -> None:
        now = datetime.utcnow()
        rows = [
            ("movie", 1, {"popcornmeter": {"value": 94.0}}, now),
            ("show", 2, {"imdb": {"value": 8.0}}, now),            # no popcornmeter -> missing
            ("movie", 3, {"popcornmeter": {"value": 50.0}}, now - timedelta(days=2)),  # stale
        ]
        db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
        values, stale, missing = await er.cached_map(db, "popcornmeter")
        self.assertEqual(values, {"m1": 94.0, "m3": 50.0})
        self.assertEqual(stale, ["m3"])
        self.assertEqual(missing, ["s2"])


if __name__ == "__main__":
    unittest.main()
