import os
import unittest
from unittest.mock import AsyncMock, patch

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


class CircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    """After a run of connection failures, _get should short-circuit instead of
    making every subsequent caller re-discover that TMDB is down (one slow
    failure per page load instead of dozens)."""

    def setUp(self) -> None:
        tmdb._cache._store.clear()
        tmdb._breaker_fail_count = 0
        tmdb._breaker_open_until = 0.0

    tearDown = setUp

    def _transport(self, *, fail: bool, count: list[int]):
        def handler(request: httpx.Request) -> httpx.Response:
            count.append(1)
            if fail:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json={"id": 1})
        return httpx.MockTransport(handler)

    async def _run(self, transport, coro_factory):
        with patch.object(tmdb.httpx, "AsyncClient",
                          side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)), \
             patch.object(tmdb.asyncio, "sleep", new_callable=AsyncMock):
            return await coro_factory()

    async def test_opens_after_threshold_then_short_circuits(self) -> None:
        calls: list[int] = []
        transport = self._transport(fail=True, count=calls)
        for _ in range(tmdb._BREAKER_THRESHOLD):
            with self.assertRaises(httpx.ConnectError):
                await self._run(transport, lambda: tmdb.get_show(1, api_key="k", cache_ttl=None))
        calls_before = len(calls)

        # Breaker is now open: no HTTP attempt, distinct exception type.
        with self.assertRaises(tmdb.TMDBUnavailable):
            await self._run(transport, lambda: tmdb.get_show(2, api_key="k", cache_ttl=None))
        self.assertEqual(len(calls), calls_before)

    async def test_success_resets_the_breaker(self) -> None:
        tmdb._breaker_fail_count = tmdb._BREAKER_THRESHOLD - 1
        ok: list[int] = []
        await self._run(self._transport(fail=False, count=ok), lambda: tmdb.get_show(1, api_key="k", cache_ttl=None))
        self.assertEqual(tmdb._breaker_fail_count, 0)

    async def test_429_does_not_trip_the_breaker(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        transport = httpx.MockTransport(handler)
        with self.assertRaises(httpx.HTTPStatusError):
            await self._run(transport, lambda: tmdb.get_show(1, api_key="k", cache_ttl=None))
        self.assertEqual(tmdb._breaker_fail_count, 0)
        self.assertFalse(tmdb._breaker_blocked())


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


class MetadataLanguageParamTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #235: Explore cards showed TMDB's default English
    title/poster because the list/discover wrappers never forwarded the
    user's Metadata Language. language must be omitted (not sent as empty)
    when unset, so those requests stay byte-identical to before and keep
    sharing the same response cache entries."""

    def setUp(self) -> None:
        tmdb._cache._store.clear()

    def _capturing_handler(self, captured: list[dict]):
        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.url.params))
            return httpx.Response(200, json={"results": [], "page": 1, "total_pages": 1})
        return handler

    async def _assert_language_param(self, coro_factory):
        captured: list[dict] = []
        transport = httpx.MockTransport(self._capturing_handler(captured))
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await coro_factory(None)
        self.assertNotIn("language", captured[0])

        captured.clear()
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await coro_factory("pt-PT")
        self.assertEqual(captured[0]["language"], "pt-PT")

    async def test_get_trending_movies(self) -> None:
        await self._assert_language_param(lambda lang: tmdb.get_trending_movies(api_key="key", language=lang))

    async def test_get_trending_shows(self) -> None:
        await self._assert_language_param(lambda lang: tmdb.get_trending_shows(api_key="key", language=lang))

    async def test_get_popular_movies(self) -> None:
        await self._assert_language_param(lambda lang: tmdb.get_popular_movies(api_key="key", language=lang))

    async def test_get_top_rated_movies(self) -> None:
        await self._assert_language_param(lambda lang: tmdb.get_top_rated_movies(api_key="key", language=lang))

    async def test_get_popular_shows(self) -> None:
        await self._assert_language_param(lambda lang: tmdb.get_popular_shows(api_key="key", language=lang))

    async def test_get_top_rated_shows(self) -> None:
        await self._assert_language_param(lambda lang: tmdb.get_top_rated_shows(api_key="key", language=lang))

    async def test_discover_movies(self) -> None:
        await self._assert_language_param(lambda lang: tmdb.discover_movies(api_key="key", language=lang))

    async def test_discover_shows(self) -> None:
        await self._assert_language_param(lambda lang: tmdb.discover_shows(api_key="key", language=lang))


class ExtractCreditsStingersTests(unittest.TestCase):
    """#319 - a movie's mid/post-credits scene is exposed via TMDB's
    community-added keywords, not a dedicated field."""

    def test_no_keywords_returns_false_false(self) -> None:
        self.assertEqual(tmdb.extract_credits_stingers({}), (False, False))

    def test_unrelated_keywords_return_false_false(self) -> None:
        data = {"keywords": {"keywords": [{"id": 1, "name": "superhero"}]}}
        self.assertEqual(tmdb.extract_credits_stingers(data), (False, False))

    def test_detects_mid_credits_stinger(self) -> None:
        data = {"keywords": {"keywords": [{"id": 1, "name": "duringcreditsstinger"}]}}
        self.assertEqual(tmdb.extract_credits_stingers(data), (True, False))

    def test_detects_post_credits_stinger(self) -> None:
        data = {"keywords": {"keywords": [{"id": 2, "name": "aftercreditsstinger"}]}}
        self.assertEqual(tmdb.extract_credits_stingers(data), (False, True))

    def test_detects_both(self) -> None:
        data = {"keywords": {"keywords": [
            {"id": 1, "name": "duringcreditsstinger"},
            {"id": 2, "name": "aftercreditsstinger"},
        ]}}
        self.assertEqual(tmdb.extract_credits_stingers(data), (True, True))


class DiscoverStudioParamsTests(unittest.IsolatedAsyncioTestCase):
    """with_networks / with_companies must reach TMDB's discover endpoints, and
    get_network / get_company must hit the right paths - these power the
    /network/{id} and /studio/{id} browse pages (#358 follow-up)."""

    def setUp(self) -> None:
        tmdb._cache._store.clear()

    def _capturing_transport(self, seen: list[httpx.Request]):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": 1, "name": "ITV", "results": []})
        return httpx.MockTransport(handler)

    async def _run(self, coro_factory):
        seen: list[httpx.Request] = []
        transport = self._capturing_transport(seen)
        with patch.object(
            tmdb.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
        ):
            await coro_factory()
        return seen[-1]

    async def test_discover_shows_forwards_with_networks_and_companies(self) -> None:
        req = await self._run(lambda: tmdb.discover_shows(with_networks=9, api_key="k"))
        self.assertEqual(req.url.path, "/3/discover/tv")
        self.assertEqual(req.url.params.get("with_networks"), "9")

        req = await self._run(lambda: tmdb.discover_shows(with_companies=1957, api_key="k"))
        self.assertEqual(req.url.params.get("with_companies"), "1957")

    async def test_discover_movies_forwards_with_companies(self) -> None:
        req = await self._run(lambda: tmdb.discover_movies(with_companies=1957, api_key="k"))
        self.assertEqual(req.url.path, "/3/discover/movie")
        self.assertEqual(req.url.params.get("with_companies"), "1957")

    async def test_discover_shows_omits_studio_params_when_unset(self) -> None:
        req = await self._run(lambda: tmdb.discover_shows(api_key="k"))
        self.assertNotIn("with_networks", req.url.params)
        self.assertNotIn("with_companies", req.url.params)

    async def test_vote_count_min_zero_is_honoured(self) -> None:
        req = await self._run(lambda: tmdb.discover_shows(with_networks=9, vote_count_min=0, api_key="k"))
        self.assertEqual(req.url.params.get("vote_count.gte"), "0")

    async def test_get_network_and_get_company_paths(self) -> None:
        req = await self._run(lambda: tmdb.get_network(9, api_key="k"))
        self.assertEqual(req.url.path, "/3/network/9")

        req = await self._run(lambda: tmdb.get_company(1957, api_key="k"))
        self.assertEqual(req.url.path, "/3/company/1957")

    async def test_search_company_hits_search_company(self) -> None:
        req = await self._run(lambda: tmdb.search_company("A24", api_key="k"))
        self.assertEqual(req.url.path, "/3/search/company")
        self.assertEqual(req.url.params.get("query"), "A24")

    async def test_episode_group_paths(self) -> None:
        req = await self._run(lambda: tmdb.get_episode_groups(1399, api_key="k"))
        self.assertEqual(req.url.path, "/3/tv/1399/episode_groups")

        req = await self._run(lambda: tmdb.get_episode_group("5f9abcd", api_key="k"))
        self.assertEqual(req.url.path, "/3/tv/episode_group/5f9abcd")


if __name__ == "__main__":
    unittest.main()
