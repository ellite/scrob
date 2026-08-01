import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType
from routers import compat


class _Result:
    def __init__(self, items):
        self.items = items

    def scalars(self):
        return self

    def all(self):
        return self.items


class _FakeSession:
    def __init__(self, list_row, rows):
        self.list_row = list_row
        self.execute = AsyncMock(return_value=_Result(rows))

    async def get(self, model, list_id):
        return self.list_row


def _series_media(tmdb_id: int = 42, title: str = "A Show") -> SimpleNamespace:
    return SimpleNamespace(
        tmdb_id=tmdb_id, title=title, original_title=None,
        overview=None, release_date=None, runtime=None, status=None,
    )


class MissingYearTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for issue #99: Radarr/Sonarr deserialize `year` into a
    non-nullable int, so a null year on any one item aborts the whole import."""

    async def test_radarr_list_defaults_missing_year_to_zero(self) -> None:
        user = SimpleNamespace(id=1)
        lst = SimpleNamespace(user_id=1)
        media = SimpleNamespace(
            tmdb_id=42, title="No Release Date", original_title=None,
            overview=None, release_date=None, runtime=None, status=None,
        )
        db = _FakeSession(lst, [media])

        result = await compat.radarr_list(list_id=1, user=user, db=db)

        self.assertEqual(result[0]["year"], 0)

    async def test_sonarr_list_defaults_missing_year_to_zero(self) -> None:
        user = SimpleNamespace(id=1)
        lst = SimpleNamespace(user_id=1)
        db = _FakeSession(lst, [(_series_media(), None, None)])

        with patch("routers.media.get_user_tmdb_key", new=AsyncMock(return_value=None)), \
             patch("core.tmdb.get_external_ids", new=AsyncMock(return_value={})):
            result = await compat.sonarr_list(list_id=1, user=user, db=db)

        self.assertEqual(result[0]["year"], 0)


class SonarrTvdbResolutionTests(unittest.IsolatedAsyncioTestCase):
    """Sonarr import lists require a valid tvdbId. Resolve it from the Show
    column, the cached TMDB payload, or a live TMDB external_ids lookup."""

    async def test_uses_show_tvdb_id_column_without_tmdb_call(self) -> None:
        user = SimpleNamespace(id=1)
        lst = SimpleNamespace(user_id=1)
        db = _FakeSession(lst, [(_series_media(tmdb_id=93870), 393926, None)])

        with patch("core.tmdb.get_external_ids", new=AsyncMock()) as m:
            result = await compat.sonarr_list(list_id=1, user=user, db=db)

        self.assertEqual(result[0]["tvdbId"], 393926)
        m.assert_not_awaited()

    async def test_falls_back_to_cached_tmdb_external_ids(self) -> None:
        user = SimpleNamespace(id=1)
        lst = SimpleNamespace(user_id=1)
        tmdb_data = {"external_ids": {"tvdb_id": 328487}}
        db = _FakeSession(lst, [(_series_media(tmdb_id=62479), None, tmdb_data)])

        with patch("core.tmdb.get_external_ids", new=AsyncMock()) as m:
            result = await compat.sonarr_list(list_id=1, user=user, db=db)

        self.assertEqual(result[0]["tvdbId"], 328487)
        m.assert_not_awaited()

    async def test_falls_back_to_tmdb_lookup_and_omits_when_unresolved(self) -> None:
        user = SimpleNamespace(id=1)
        lst = SimpleNamespace(user_id=1)
        db = _FakeSession(lst, [
            (_series_media(tmdb_id=93870, title="Resolvable"), None, None),
            (_series_media(tmdb_id=999999, title="Unlisted"), None, None),
        ])

        async def fake_external_ids(tmdb_id, type, api_key=None):
            return {"tvdb_id": 393926} if tmdb_id == 93870 else {}

        with patch("routers.media.get_user_tmdb_key", new=AsyncMock(return_value="k")), \
             patch("core.tmdb.get_external_ids", side_effect=fake_external_ids) as m:
            result = await compat.sonarr_list(list_id=1, user=user, db=db)

        self.assertEqual(result[0]["tvdbId"], 393926)
        self.assertNotIn("tvdbId", result[1])
        self.assertEqual(m.await_count, 2)

    async def test_tmdb_lookup_fan_out_is_capped_by_concurrency_limit(self) -> None:
        user = SimpleNamespace(id=1)
        lst = SimpleNamespace(user_id=1)
        item_count = compat.TMDB_CONCURRENCY * 3
        db = _FakeSession(lst, [
            (_series_media(tmdb_id=1000 + i, title=f"Show {i}"), None, None)
            for i in range(item_count)
        ])

        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def fake_external_ids(tmdb_id, type, api_key=None):
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1
            return {"tvdb_id": tmdb_id}

        with patch("routers.media.get_user_tmdb_key", new=AsyncMock(return_value="k")), \
             patch("core.tmdb.get_external_ids", side_effect=fake_external_ids):
            result = await compat.sonarr_list(list_id=1, user=user, db=db)

        self.assertEqual(len(result), item_count)
        self.assertEqual(max_in_flight, compat.TMDB_CONCURRENCY)


if __name__ == "__main__":
    unittest.main()
