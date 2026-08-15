import os
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import tmdb


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class GetShowCacheBypassTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for: "Refresh Metadata" calling tmdb.get_show/get_season/
    get_movie/get_episode with no cache_ttl override meant a user-initiated
    refresh could silently return whatever response was already sitting in
    the shared 30-minute cache (e.g. from just browsing the same title
    moments earlier), making the button appear to work while doing nothing."""

    def setUp(self) -> None:
        tmdb._cache._store.clear()

    def _counting_handler(self, request_count: list[int]):
        def handler(request: httpx.Request) -> httpx.Response:
            request_count.append(1)
            return httpx.Response(200, json={"name": "Show", "id": 1})
        return handler

    async def test_default_cache_ttl_serves_second_call_from_cache(self) -> None:
        requests: list[int] = []
        transport = httpx.MockTransport(self._counting_handler(requests))
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tmdb.get_show(1399, api_key="key")
            await tmdb.get_show(1399, api_key="key")

        self.assertEqual(len(requests), 1)

    async def test_cache_ttl_none_always_hits_the_network(self) -> None:
        requests: list[int] = []
        transport = httpx.MockTransport(self._counting_handler(requests))
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tmdb.get_show(1399, api_key="key")  # populates the cache
            await tmdb.get_show(1399, api_key="key", cache_ttl=None)  # must not read it
            await tmdb.get_show(1399, api_key="key", cache_ttl=None)  # must not populate it either

        self.assertEqual(len(requests), 3)

    async def test_get_season_and_get_movie_and_get_episode_accept_cache_ttl(self) -> None:
        # Confirms all three TMDB wrappers used by the refresh paths accept
        # cache_ttl and actually reach the network on every call when None,
        # not just get_show.
        requests: list[int] = []
        transport = httpx.MockTransport(self._counting_handler(requests))
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tmdb.get_season(1399, 1, api_key="key", cache_ttl=None)
            await tmdb.get_season(1399, 1, api_key="key", cache_ttl=None)
            await tmdb.get_movie(550, api_key="key", cache_ttl=None)
            await tmdb.get_movie(550, api_key="key", cache_ttl=None)
            await tmdb.get_episode(1399, 1, 1, api_key="key", cache_ttl=None)
            await tmdb.get_episode(1399, 1, 1, api_key="key", cache_ttl=None)

        self.assertEqual(len(requests), 6)


class DiscoverGenreIdsTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for multi-genre selection on Explore Movies/Shows:
    TMDB's discover endpoints OR multiple genres together via a "|"-joined
    with_genres value in a single request - genre_ids is the new multi-value
    param for that; genre_id (singular) is kept for existing callers."""

    def setUp(self) -> None:
        tmdb._cache._store.clear()

    def _capturing_handler(self, captured: list[dict]):
        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.url.params))
            return httpx.Response(200, json={"results": [], "page": 1, "total_pages": 1})
        return handler

    async def test_discover_movies_ors_multiple_genre_ids(self) -> None:
        captured: list[dict] = []
        transport = httpx.MockTransport(self._capturing_handler(captured))
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tmdb.discover_movies(genre_ids=[28, 35], api_key="key")

        self.assertEqual(captured[0]["with_genres"], "28|35")

    async def test_discover_shows_ors_multiple_genre_ids(self) -> None:
        captured: list[dict] = []
        transport = httpx.MockTransport(self._capturing_handler(captured))
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tmdb.discover_shows(genre_ids=[16, 10759], api_key="key")

        self.assertEqual(captured[0]["with_genres"], "16|10759")

    async def test_genre_ids_takes_priority_over_genre_id(self) -> None:
        captured: list[dict] = []
        transport = httpx.MockTransport(self._capturing_handler(captured))
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tmdb.discover_movies(genre_id=99, genre_ids=[28], api_key="key")

        self.assertEqual(captured[0]["with_genres"], "28")

    async def test_single_genre_id_still_works_for_existing_callers(self) -> None:
        captured: list[dict] = []
        transport = httpx.MockTransport(self._capturing_handler(captured))
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tmdb.discover_movies(genre_id=28, api_key="key")

        self.assertEqual(captured[0]["with_genres"], "28")


if __name__ == "__main__":
    unittest.main()
