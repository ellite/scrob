import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from fastapi import HTTPException

from models.base import MediaType
from routers import shows


class _Scalars:
    def __init__(self, item=None):
        self.item = item

    def first(self):
        if isinstance(self.item, list):
            return self.item[0] if self.item else None
        return self.item

    def all(self):
        if isinstance(self.item, list):
            return self.item
        return [] if self.item is None else [self.item]


class _Result:
    def __init__(self, item=None):
        self.item = item

    def scalars(self):
        return _Scalars(self.item)

    def scalar_one_or_none(self):
        return self.item


class _FakeSession:
    """Queues results for db.execute() in call order - mirrors the pattern
    already established in tests/test_history.py."""

    def __init__(self, results):
        self.execute = AsyncMock(side_effect=[_Result(item) for item in results])


class _NestedTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSessionWithNesting(_FakeSession):
    """Adds begin_nested()/flush()/commit() support for tests that exercise
    apply_media_change_safely - mirrors tests/test_history.py's _FakeSession."""

    def __init__(self, results):
        super().__init__(results)
        self.flush = AsyncMock()
        self.commit = AsyncMock()

    def begin_nested(self):
        return _NestedTxn()


class GetEpisodeDetailTvdbFallbackMappingTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #186's follow-up: when TMDB doesn't have an
    episode and the show has a TVDB match, get_episode_detail must translate
    the URL's TMDB-style season/episode into the show's real TVDB position
    via EpisodeOrderMapping before delegating to get_tvdb_episode - reusing
    the same numbers unchanged returns a DIFFERENT, wrong episode's data
    instead of a clear error (reported as "the fix made it worse")."""

    def _user(self):
        return SimpleNamespace(id=7)

    async def test_mapped_episode_translates_to_real_tvdb_position(self) -> None:
        show = SimpleNamespace(tmdb_id=980001, tvdb_id=980002)
        mapping = SimpleNamespace(tvdb_season_number=4, tvdb_episode_number=12)
        # Query order: 1) show lookup, 2) EpisodeOrderMapping lookup.
        db = _FakeSession([show, mapping])

        tvdb_calls = []

        async def fake_get_tvdb_episode(tvdb_id, season_number, episode_number, db_arg, user_arg):
            tvdb_calls.append((tvdb_id, season_number, episode_number))
            return {"title": "the real episode"}

        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("routers.shows.tmdb.get_episode", AsyncMock(side_effect=Exception("404 Not Found"))), \
             patch("routers.shows.get_tvdb_episode", fake_get_tvdb_episode):
            result = await shows.get_episode_detail(980001, 1, 1, db, self._user())

        self.assertEqual(tvdb_calls, [(980002, 4, 12)])
        self.assertEqual(result["title"], "the real episode")

    async def test_unmapped_episode_raises_instead_of_guessing(self) -> None:
        show = SimpleNamespace(tmdb_id=980001, tvdb_id=980002)
        # Query order: 1) show lookup, 2) EpisodeOrderMapping lookup (none found).
        db = _FakeSession([show, None])

        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("routers.shows.tmdb.get_episode", AsyncMock(side_effect=Exception("404 Not Found"))), \
             patch("routers.shows.get_tvdb_episode", AsyncMock()) as tvdb_mock:
            with self.assertRaises(HTTPException) as ctx:
                await shows.get_episode_detail(980001, 9, 9, db, self._user())

        self.assertEqual(ctx.exception.status_code, 404)
        tvdb_mock.assert_not_awaited()


class RefreshShowMetadataTvdbFallbackCorruptionTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #186's second follow-up: refresh_show_metadata's
    TVDB fallback has the same "reuse possibly-TMDB-canonical numbers as TVDB
    query keys" flaw as get_episode_detail's did, except here it PERSISTS the
    wrong episode's data instead of just displaying it. A temporary TMDB
    failure for one season (unrelated to real TVDB/TMDB divergence) must not
    silently overwrite an already-correct, TMDB-sourced episode."""

    def _show(self):
        return SimpleNamespace(id=1, tmdb_id=980001, tvdb_id=980002, title="Show")

    def _tmdb_show_data(self):
        return {"name": "Show", "seasons": [{"season_number": 1, "episode_count": 10, "name": "Season 1"}]}

    async def test_tmdb_sourced_episode_is_not_overwritten_by_coincidental_tvdb_match(self) -> None:
        show = self._show()
        good_ep = SimpleNamespace(
            id=101, media_type=MediaType.episode, season_number=1, episode_number=1, show_id=None,
            title="Correct Existing Title", overview="Correct overview",
            tmdb_id=555555, tmdb_data={"runtime": 42, "cast": []},  # NOT tvdb-sourced
        )
        # Query order: 1) show, 2) linked episodes, 3) orphans.
        db = _FakeSessionWithNesting([show, [good_ep], []])

        wrong_tvdb_ep = {"id": 999999, "seasonNumber": 1, "number": 1, "name": "WRONG Episode"}

        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.tmdb.get_show", AsyncMock(return_value=self._tmdb_show_data())), \
             patch("routers.shows.tmdb.get_season", AsyncMock(side_effect=Exception("temporary TMDB failure"))), \
             patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value="tvdb-key")), \
             patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("routers.shows.tvdb_client.get_series_episodes", AsyncMock(return_value=[wrong_tvdb_ep])), \
             patch("routers.shows.refresh_technical_data", AsyncMock()):
            await shows.refresh_show_metadata(980001, db, SimpleNamespace(id=7))

        self.assertEqual(good_ep.title, "Correct Existing Title")
        self.assertEqual(good_ep.tmdb_id, 555555)

    async def test_tvdb_sourced_episode_still_refreshes_normally(self) -> None:
        show = self._show()
        tvdb_ep = SimpleNamespace(
            id=102, media_type=MediaType.episode, season_number=1, episode_number=2, show_id=show.id,
            title="Stale Title", overview="stale", tmdb_id=888888,
            tmdb_data={"runtime": 20, "tvdb_episode_id": 888888, "source": "tvdb"},
        )
        db = _FakeSessionWithNesting([show, [tvdb_ep], []])

        raw_tvdb_ep = {
            "id": 888888, "seasonNumber": 1, "number": 2, "name": "Refreshed Title",
            "overview": "refreshed", "aired": "2020-01-01", "runtime": 25, "image": None,
        }

        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.tmdb.get_show", AsyncMock(return_value=self._tmdb_show_data())), \
             patch("routers.shows.tmdb.get_season", AsyncMock(side_effect=Exception("temporary TMDB failure"))), \
             patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value="tvdb-key")), \
             patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("routers.shows.tvdb_client.get_series_episodes", AsyncMock(return_value=[raw_tvdb_ep])), \
             patch("routers.shows.refresh_technical_data", AsyncMock()):
            await shows.refresh_show_metadata(980001, db, SimpleNamespace(id=7))

        self.assertEqual(tvdb_ep.title, "Refreshed Title")


if __name__ == "__main__":
    unittest.main()
