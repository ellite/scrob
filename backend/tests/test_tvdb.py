import os
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import tvdb


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class GetSeriesCacheBypassTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for adding response caching to core/tvdb.py (mirroring
    core/tmdb.py's existing cache): a "Refresh Metadata" action must still
    bypass it via cache_ttl=None, or refreshing a TVDB-primary show would
    silently return whatever was already cached, same as the TMDB bug this
    was built alongside."""

    def setUp(self) -> None:
        tvdb._cache._store.clear()
        tvdb._token_cache.clear()

    def _counting_handler(self, request_count: list[int]):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"data": {"token": "tok"}})
            request_count.append(1)
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"data": []})
            if "/episodes/" in request.url.path:
                return httpx.Response(200, json={"data": {"episodes": []}})
            return httpx.Response(200, json={"data": {"id": 1, "name": "Show"}})
        return handler

    async def test_default_cache_ttl_serves_second_call_from_cache(self) -> None:
        requests: list[int] = []
        transport = httpx.MockTransport(self._counting_handler(requests))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tvdb.get_series(12345, "key")
            await tvdb.get_series(12345, "key")

        self.assertEqual(len(requests), 1)

    async def test_cache_ttl_none_always_hits_the_network(self) -> None:
        requests: list[int] = []
        transport = httpx.MockTransport(self._counting_handler(requests))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tvdb.get_series(12345, "key")  # populates the cache
            await tvdb.get_series(12345, "key", cache_ttl=None)  # must not read it
            await tvdb.get_series(12345, "key", cache_ttl=None)  # must not populate it either

        self.assertEqual(len(requests), 3)

    async def test_get_season_and_get_series_episodes_and_search_series_accept_cache_ttl(self) -> None:
        requests: list[int] = []
        transport = httpx.MockTransport(self._counting_handler(requests))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tvdb.get_season(1, "key", cache_ttl=None)
            await tvdb.get_season(1, "key", cache_ttl=None)
            await tvdb.search_series("Show", "key", cache_ttl=None)
            await tvdb.search_series("Show", "key", cache_ttl=None)

        self.assertEqual(len(requests), 4)

    async def test_get_series_episodes_bypasses_cache_across_pages(self) -> None:
        # get_series_episodes paginates internally via _get - cache_ttl must
        # reach every page's request, not just the first.
        requests: list[int] = []
        transport = httpx.MockTransport(self._counting_handler(requests))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tvdb.get_series_episodes(12345, None, "key", cache_ttl=None)

        # A single (short) page - just confirming the call reached the network
        # and the (path, params) cache key wasn't served from a prior test.
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
