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

    async def test_season_type_is_threaded_into_the_path(self) -> None:
        # #174: get_series_episodes fetches a non-aired ordering via the
        # season-type path segment.
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"data": {"token": "tok"}})
            seen.append(request.url.path)
            return httpx.Response(200, json={"data": {"episodes": []}})

        transport = httpx.MockTransport(handler)
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tvdb.get_series_episodes(121361, 1, "key", cache_ttl=None)
            await tvdb.get_series_episodes(121361, None, "key", season_type="dvd", cache_ttl=None)

        self.assertTrue(seen[0].endswith("/series/121361/episodes/official"))
        self.assertTrue(seen[1].endswith("/series/121361/episodes/dvd"))


class SubscriberPinTests(unittest.IsolatedAsyncioTestCase):
    """#322/#325: a subscriber-supported TVDB key must be sent to /login with
    its account PIN; a free project key sends no PIN."""

    def setUp(self) -> None:
        tvdb._cache._store.clear()
        tvdb._token_cache.clear()
        tvdb._subscriber_pins.clear()

    def _login_capturing_handler(self, bodies: list[dict]):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                import json
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"data": {"token": "tok"}})
            return httpx.Response(200, json={"data": {"id": 1, "name": "Show"}})
        return handler

    async def test_registered_pin_is_sent_on_login(self) -> None:
        bodies: list[dict] = []
        transport = httpx.MockTransport(self._login_capturing_handler(bodies))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            tvdb.set_subscriber_pin("key", "PIN123")
            await tvdb.get_series(1, "key")
        self.assertEqual(bodies, [{"apikey": "key", "pin": "PIN123"}])

    async def test_no_pin_means_project_key_login(self) -> None:
        bodies: list[dict] = []
        transport = httpx.MockTransport(self._login_capturing_handler(bodies))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await tvdb.get_series(1, "key")
        self.assertEqual(bodies, [{"apikey": "key"}])

    async def test_pin_change_forces_a_fresh_login(self) -> None:
        bodies: list[dict] = []
        transport = httpx.MockTransport(self._login_capturing_handler(bodies))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            tvdb.set_subscriber_pin("key", "OLD")
            await tvdb.get_series(1, "key")
            tvdb.set_subscriber_pin("key", "NEW")
            await tvdb.get_series(2, "key")
        self.assertEqual([b.get("pin") for b in bodies], ["OLD", "NEW"])

    async def test_set_subscriber_pin_blank_clears_it(self) -> None:
        tvdb.set_subscriber_pin("key", "PIN")
        tvdb.set_subscriber_pin("key", "")
        self.assertNotIn("key", tvdb._subscriber_pins)

    async def test_validate_api_key_uses_explicit_pin_over_registry(self) -> None:
        bodies: list[dict] = []
        transport = httpx.MockTransport(self._login_capturing_handler(bodies))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            tvdb.set_subscriber_pin("key", "REGISTERED")
            ok = await tvdb.validate_api_key("key", pin="TYPED")
        self.assertTrue(ok)
        self.assertEqual(bodies[0]["pin"], "TYPED")


class SearchSeriesYearFallbackTests(unittest.IsolatedAsyncioTestCase):
    """#364: TVDB's /search sometimes omits "year" even though the result has
    a first_air_time (and, separately, sometimes has neither at all) - two
    remakes sharing a title then render identically and can't be told apart.
    search_series must fall back to first_air_time, and always include
    tvdb_id/status/network so there's an unambiguous identifier either way."""

    def setUp(self) -> None:
        tvdb._cache._store.clear()
        tvdb._token_cache.clear()

    def _handler(self, items: list[dict]):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"data": {"token": "tok"}})
            return httpx.Response(200, json={"data": items})
        return handler

    async def _search(self, items: list[dict]) -> list[dict]:
        transport = httpx.MockTransport(self._handler(items))
        with patch.object(
            tvdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            return await tvdb.search_series("query", "key")

    async def test_year_used_when_present(self) -> None:
        results = await self._search([
            {"tvdb_id": "1", "name": "Show", "year": "1989", "first_air_time": "1989-04-15"},
        ])
        self.assertEqual(results[0]["year"], "1989")

    async def test_falls_back_to_first_air_time_when_year_missing(self) -> None:
        results = await self._search([
            {"tvdb_id": "2", "name": "Show (2024)", "first_air_time": "2024-10-06"},
        ])
        self.assertEqual(results[0]["year"], "2024")

    async def test_year_is_none_when_neither_field_is_present(self) -> None:
        # A real TVDB result (Tenali Rama, id 270090) has neither - must not
        # crash slicing a missing string, and must not fabricate a year.
        results = await self._search([{"tvdb_id": "3", "name": "Show"}])
        self.assertIsNone(results[0]["year"])

    async def test_tvdb_id_status_and_network_always_pass_through(self) -> None:
        # These are what let two identically-titled, year-less remakes still
        # be told apart in the remap UI.
        results = await self._search([
            {"tvdb_id": "4", "name": "Show", "status": "Ended", "network": "Fuji TV"},
        ])
        self.assertEqual(results[0]["tvdb_id"], 4)
        self.assertEqual(results[0]["status"], "Ended")
        self.assertEqual(results[0]["network"], "Fuji TV")


if __name__ == "__main__":
    unittest.main()
