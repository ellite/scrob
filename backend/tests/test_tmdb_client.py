import asyncio
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx

from core import tmdb


class PooledClientTests(unittest.IsolatedAsyncioTestCase):
    """core.tmdb reuses one httpx client per event loop instead of building a
    fresh one — and therefore a fresh TCP+TLS handshake — for every request."""

    async def test_the_same_client_is_reused_within_a_loop(self) -> None:
        first = tmdb._get_client()
        second = tmdb._get_client()
        self.assertIs(first, second)

    async def test_client_is_not_shared_across_event_loops_a(self) -> None:
        """Paired with ..._b. Every IsolatedAsyncioTestCase gets a fresh loop,
        so a naive module-level singleton would be reused on a closed loop and
        raise 'Event loop is closed'. Registry entries are keyed by loop and
        stale ones pruned, so this stays at one entry."""
        tmdb._get_client()
        self.assertEqual(len(tmdb._clients), 1)

    async def test_client_is_not_shared_across_event_loops_b(self) -> None:
        client = tmdb._get_client()
        self.assertEqual(len(tmdb._clients), 1)
        self.assertIs(tmdb._clients[asyncio.get_running_loop()], client)
        self.assertFalse(client.is_closed)

    async def test_a_global_httpx_patch_cannot_capture_the_tmdb_client(self) -> None:
        """~35 tests patch httpx.AsyncClient process-wide to inject a
        MockTransport. tmdb captures the real class at import so it can never
        build its client from another service's transport and memoise it."""
        await tmdb.aclose()

        sentinel_transport = httpx.MockTransport(
            lambda request: httpx.Response(404, json={"message": "wrong transport"})
        )
        real = httpx.AsyncClient
        with patch.object(
            httpx, "AsyncClient",
            side_effect=lambda **kw: real(transport=sentinel_transport, **kw),
        ):
            client = tmdb._get_client()

        self.assertIsInstance(client, real)
        self.assertNotIn("wrong transport", repr(client._transport))
        await tmdb.aclose()

    async def test_aclose_releases_the_client(self) -> None:
        client = tmdb._get_client()
        await tmdb.aclose()
        self.assertTrue(client.is_closed)
        self.assertEqual(tmdb._clients, {})

    async def test_get_rebuilds_a_client_after_shutdown(self) -> None:
        """A retry arriving after aclose() must not hit 'client is closed'."""
        await tmdb.aclose()
        first = tmdb._get_client()
        await tmdb.aclose()
        second = tmdb._get_client()
        self.assertIsNot(first, second)
        self.assertFalse(second.is_closed)
        await tmdb.aclose()

    async def test_get_still_honours_retry_after_on_429(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"id": 550})

        real = httpx.AsyncClient
        client = real(transport=httpx.MockTransport(handler))
        with patch.object(tmdb, "_get_client", lambda: client):
            data = await tmdb._get("https://api.themoviedb.org/3/movie/550", cache_ttl=None)

        self.assertEqual(data, {"id": 550})
        self.assertEqual(len(calls), 2)
        await client.aclose()


class TTLCacheTests(unittest.TestCase):
    def test_eviction_is_least_recently_used_not_insertion_order(self) -> None:
        """A big sync streams thousands of one-shot season keys through this
        cache; under FIFO those evicted the repeatedly-read show entries just
        as fast."""
        cache = tmdb._TTLCache(maxsize=2)
        cache.set(("hot",), {"v": 1}, 60)
        cache.set(("cold",), {"v": 2}, 60)

        self.assertEqual(cache.get(("hot",)), {"v": 1})  # touch it
        cache.set(("new",), {"v": 3}, 60)                # evicts the LRU entry

        self.assertEqual(cache.get(("hot",)), {"v": 1})
        self.assertIsNone(cache.get(("cold",)))
        self.assertEqual(cache.get(("new",)), {"v": 3})

    def test_expired_entries_are_dropped_on_read(self) -> None:
        cache = tmdb._TTLCache(maxsize=8)
        cache.set(("k",), {"v": 1}, -1)
        self.assertIsNone(cache.get(("k",)))

    def test_maxsize_is_respected(self) -> None:
        cache = tmdb._TTLCache(maxsize=3)
        for i in range(10):
            cache.set((i,), {"v": i}, 60)
        self.assertEqual(len(cache._store), 3)


if __name__ == "__main__":
    unittest.main()
