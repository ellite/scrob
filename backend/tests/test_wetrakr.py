import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx

from core import wetrakr
from core.wetrakr import WeTrakrAuthError

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched(handler):
    transport = httpx.MockTransport(handler)
    return patch.object(
        wetrakr.httpx, "AsyncClient",
        side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
    )


class PollDeviceTokenTests(unittest.IsolatedAsyncioTestCase):
    """WeTrakr's device/token poll uses more terminal status codes than Trakt's
    (404/409/410/418), each meaning something different to the user — a bare
    raise_for_status() would collapse them all into the same generic error."""

    async def test_200_returns_token_dict(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "access_token": "at", "refresh_token": "rt", "token_type": "Bearer", "expires_in": 604800,
            })

        with _patched(handler):
            result = await wetrakr.poll_device_token("dc")
        self.assertEqual(result["access_token"], "at")

    async def test_400_is_pending(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "pending"})

        with _patched(handler):
            self.assertIsNone(await wetrakr.poll_device_token("dc"))

    async def test_400_authorization_pending_is_pending(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "authorization_pending"})

        with _patched(handler):
            self.assertIsNone(await wetrakr.poll_device_token("dc"))

    async def test_400_invalid_request_stops_polling(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_request"})

        with _patched(handler):
            with self.assertRaises(WeTrakrAuthError):
                await wetrakr.poll_device_token("dc")

    async def test_429_is_treated_as_pending(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "slow_down"})

        with _patched(handler):
            self.assertIsNone(await wetrakr.poll_device_token("dc"))

    async def test_404_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not_found"})

        with _patched(handler):
            with self.assertRaises(WeTrakrAuthError):
                await wetrakr.poll_device_token("dc")

    async def test_410_expired_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(410, json={"error": "expired"})

        with _patched(handler):
            with self.assertRaises(WeTrakrAuthError):
                await wetrakr.poll_device_token("dc")

    async def test_418_denied_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(418, json={"error": "denied"})

        with _patched(handler):
            with self.assertRaises(WeTrakrAuthError):
                await wetrakr.poll_device_token("dc")

    async def test_409_already_used_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"error": "already_used"})

        with _patched(handler):
            with self.assertRaises(WeTrakrAuthError):
                await wetrakr.poll_device_token("dc")


class RefreshAccessTokenFieldNameTests(unittest.IsolatedAsyncioTestCase):
    """WeTrakr's own docs flag this: the refresh endpoint returns the rotated
    refresh token as `new_refresh_token`, not `refresh_token` like the initial
    device-token exchange does. The client must hand back whatever the API
    actually returned, and the caller (ensure_valid_wetrakr_token) is what
    reads the right field."""

    async def test_refresh_response_carries_new_refresh_token_field(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "access_token": "new_at", "new_refresh_token": "rotated_rt",
                "token_type": "Bearer", "expires_in": 604800,
            })

        with _patched(handler):
            result = await wetrakr.refresh_access_token("old_rt")
        self.assertEqual(result["access_token"], "new_at")
        self.assertEqual(result["new_refresh_token"], "rotated_rt")
        self.assertNotIn("refresh_token", result)


class AddToWatchedBatchTests(unittest.IsolatedAsyncioTestCase):
    """Mirrors core/trakt.py's add_to_history_batch / core/simkl.py's
    add_history_batch: episodes group into shows[].seasons[].episodes[] keyed
    by show tmdb id, and a missing watched_at becomes tracked_at_unknown
    rather than a fabricated "now" date."""

    async def test_groups_episodes_by_show_and_season(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={})

        with _patched(handler):
            await wetrakr.add_to_watched_batch(
                "tok",
                movies=[(603, None)],
                episodes=[(1399, 1, 1, None), (1399, 1, 2, None)],
            )

        body = captured["body"]
        self.assertEqual(body["movies"], [{"ids": {"tmdb": 603}, "status": "watched", "tracked_at_unknown": True}])
        self.assertEqual(len(body["shows"]), 1)
        show = body["shows"][0]
        self.assertEqual(show["ids"], {"tmdb": 1399})
        self.assertEqual(len(show["seasons"]), 1)
        self.assertEqual(show["seasons"][0]["number"], 1)
        self.assertEqual([e["number"] for e in show["seasons"][0]["episodes"]], [1, 2])
        self.assertTrue(all(e.get("tracked_at_unknown") for e in show["seasons"][0]["episodes"]))
        # status is required on nested episodes too (WeTrakr 1.0.4+)
        self.assertTrue(all(e.get("status") == "watched" for e in show["seasons"][0]["episodes"]))

    async def test_noop_when_nothing_to_push(self):
        called = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, json={})

        with _patched(handler):
            await wetrakr.add_to_watched_batch("tok", [], [])
        self.assertEqual(called["count"], 0)


class SetRatingsBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_rating_clamped_and_rounded(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={})

        with _patched(handler):
            await wetrakr.set_ratings_batch("tok", movie_ratings=[(603, 10.6)], show_ratings=[(1399, 0.2)])

        body = captured["body"]
        self.assertEqual(body["movies"], [{"rating": 10, "ids": {"tmdb": 603}}])
        self.assertEqual(body["shows"], [{"rating": 1, "ids": {"tmdb": 1399}}])


class WeTrakrRatingHelperTests(unittest.TestCase):
    """The /sync/ratings/{target} response nests the user's rating under
    interactions.user.rating, which is itself an object ({rating, rated_at})
    rather than a bare number — confirmed against the live API after an
    initial (wrong) flat-number reading crashed the sync job with
    "float() argument must be a string or a real number, not 'dict'"."""

    def test_extracts_rating_and_rated_at(self):
        from routers.wetrakr import _wetrakr_user_rating
        item = {"interactions": {"user": {"rating": {"rating": 8, "rated_at": "2026-01-01T00:00:00.000Z"}}}}
        rating, rated_at = _wetrakr_user_rating(item)
        self.assertEqual(rating, 8.0)
        self.assertEqual(rated_at, "2026-01-01T00:00:00.000Z")

    def test_extracts_compact_rating_and_rated_at(self):
        from routers.wetrakr import _wetrakr_user_rating
        item = {"type": "movie", "ids": {"tmdb": 603}, "rating": 9, "rated_at": "2026-10-10T12:00:00.000Z"}
        self.assertEqual(_wetrakr_user_rating(item), (9.0, "2026-10-10T12:00:00.000Z"))

    def test_missing_interactions_returns_none(self):
        from routers.wetrakr import _wetrakr_user_rating
        self.assertEqual(_wetrakr_user_rating({}), (None, None))

    def test_flat_number_rating_is_not_mistaken_for_the_object_shape(self):
        """Regression guard: interactions.user.rating as a bare number (the
        shape this code wrongly assumed at first) must be treated as absent,
        not crash trying to .get() on an int."""
        from routers.wetrakr import _wetrakr_user_rating
        item = {"interactions": {"user": {"rating": 8}}}
        self.assertEqual(_wetrakr_user_rating(item), (None, None))

    def test_extracts_tmdb_id_from_ids(self):
        from routers.wetrakr import _wetrakr_media_tmdb_id
        self.assertEqual(_wetrakr_media_tmdb_id({"ids": {"tmdb": "603", "imdb": "tt0137523"}}), 603)
        self.assertIsNone(_wetrakr_media_tmdb_id({"ids": {}}))
        self.assertIsNone(_wetrakr_media_tmdb_id({}))


class AddItemsToListTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_movies_and_shows_by_tmdb_id(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            captured["body"] = json.loads(request.content)
            captured["path"] = request.url.path
            return httpx.Response(200, json={})

        with _patched(handler):
            await wetrakr.add_items_to_list("tok", 17599, movies=[603, 155], shows=[1399])

        self.assertEqual(captured["path"], "/sync/lists/17599/items")
        self.assertEqual(captured["body"]["movies"], [{"ids": {"tmdb": 603}}, {"ids": {"tmdb": 155}}])
        self.assertEqual(captured["body"]["shows"], [{"ids": {"tmdb": 1399}}])

    async def test_noop_when_nothing_to_add(self):
        called = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, json={})

        with _patched(handler):
            await wetrakr.add_items_to_list("tok", 17599, [], [])
        self.assertEqual(called["count"], 0)


class WriteCommentTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_comment_body(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 80055})

        with _patched(handler):
            result = await wetrakr.write_comment("tok", movie_tmdb_id=603, text="Great movie", spoiler=False)

        self.assertEqual(captured["body"], {"text": "Great movie", "spoiler": False, "movie": {"ids": {"tmdb": 603}}})
        self.assertEqual(result["id"], 80055)

    async def test_show_comment_body(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 80056})

        with _patched(handler):
            await wetrakr.write_comment("tok", show_tmdb_id=1399, text="Great show", spoiler=True)

        self.assertEqual(captured["body"], {"text": "Great show", "spoiler": True, "show": {"ids": {"tmdb": 1399}}})

    async def test_requires_a_target(self):
        with self.assertRaises(ValueError):
            await wetrakr.write_comment("tok", text="No target")


class ResolveWetrakrCommentTargetTests(unittest.IsolatedAsyncioTestCase):
    """Comments only carry WeTrakr's own numeric id for their target (unlike
    every other read in this API), so resolving one to a tmdb id needs a
    follow-up lookup — see core/wetrakr.py: get_movie/get_show/get_season/
    get_episode. These tests mock those lookups directly rather than the
    HTTP layer, since the resolver's job is the branching/caching around them."""

    async def test_movie_target(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        with patch("routers.wetrakr.wetrakr_client.get_movie", return_value={"ids": {"tmdb": 603}}) as mock_get:
            result = await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, {})
        self.assertEqual(result, ("movie", 603, None, None))
        mock_get.assert_awaited_once_with(126)

    async def test_show_target(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        with patch("routers.wetrakr.wetrakr_client.get_show", return_value={"ids": {"tmdb": 1396}}):
            result = await _resolve_wetrakr_comment_target({"target": "show", "show": {"id": 1391953}}, {})
        self.assertEqual(result, ("series", 1396, None, None))

    async def test_season_target_uses_shows_tmdb_id_and_seasons_own_number(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        season_payload = {"number": 1, "show": {"ids": {"tmdb": 1396}}}
        with patch("routers.wetrakr.wetrakr_client.get_season", return_value=season_payload):
            result = await _resolve_wetrakr_comment_target({"target": "season", "season": {"id": 135905}}, {})
        self.assertEqual(result, ("series", 1396, 1, None))

    async def test_episode_target_flat_season_number_field(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        episode_payload = {"season_number": 1, "number": 3, "show": {"ids": {"tmdb": 1396}}}
        with patch("routers.wetrakr.wetrakr_client.get_episode", return_value=episode_payload):
            result = await _resolve_wetrakr_comment_target({"target": "episode", "episode": {"id": 174660}}, {})
        self.assertEqual(result, ("episode", 1396, 1, 3))

    async def test_episode_target_falls_back_to_nested_season_object(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        episode_payload = {"season": {"number": 2}, "number": 5, "show": {"ids": {"tmdb": 1396}}}
        with patch("routers.wetrakr.wetrakr_client.get_episode", return_value=episode_payload):
            result = await _resolve_wetrakr_comment_target({"target": "episode", "episode": {"id": 174700}}, {})
        self.assertEqual(result, ("episode", 1396, 2, 5))

    async def test_person_and_list_targets_are_skipped_without_a_lookup(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        with patch("routers.wetrakr.wetrakr_client.get_movie") as mock_get:
            self.assertIsNone(await _resolve_wetrakr_comment_target({"target": "person", "person": {"id": 1}}, {}))
            self.assertIsNone(await _resolve_wetrakr_comment_target({"target": "list", "list": {"id": 1}}, {}))
        mock_get.assert_not_awaited()

    async def test_missing_title_returns_none(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        response = httpx.Response(404, request=httpx.Request("GET", "https://api.wetrakr.com/movies/126"))
        error = httpx.HTTPStatusError("not found", request=response.request, response=response)
        with patch("routers.wetrakr.wetrakr_client.get_movie", side_effect=error):
            result = await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, {})
        self.assertIsNone(result)

    async def test_transient_failure_is_raised_so_the_comment_is_retried(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        response = httpx.Response(503, request=httpx.Request("GET", "https://api.wetrakr.com/movies/126"))
        error = httpx.HTTPStatusError("unavailable", request=response.request, response=response)
        with patch("routers.wetrakr.wetrakr_client.get_movie", side_effect=error):
            with self.assertRaises(httpx.HTTPStatusError):
                await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, {})
        with patch("routers.wetrakr.wetrakr_client.get_movie", side_effect=wetrakr.WeTrakrQuotaExceeded("quota")):
            with self.assertRaises(wetrakr.WeTrakrQuotaExceeded):
                await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, {})

    async def test_caches_repeated_lookups_within_one_run(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        caches: dict[str, dict[int, dict]] = {}
        with patch("routers.wetrakr.wetrakr_client.get_movie", return_value={"ids": {"tmdb": 603}}) as mock_get:
            await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, caches)
            await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, caches)
        mock_get.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()


class WeTrakrRequestPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_is_retried_after_retry_after(self):
        import httpx
        from unittest import mock
        from core import wetrakr

        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json={"ok": True})

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with mock.patch.object(wetrakr.asyncio, "sleep", fake_sleep):
                resp = await wetrakr._send(client, "POST", "/sync/tracking", "tok", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [2.0])

    async def test_watched_push_never_allows_rewatch(self):
        import json
        import httpx
        from core import wetrakr

        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        from unittest import mock
        with mock.patch.object(wetrakr.httpx, "AsyncClient", lambda **kw: real_client(transport=transport)):
            await wetrakr.add_to_watched_batch("tok", [(1, None)], [])
        self.assertIs(bodies[0]["allow_rewatch"], False)


class WeTrakrIncrementalReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_uses_from_date_and_compact_cursor_pages(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json=[{"type": "movie", "ids": {"tmdb": 603}, "play_id": "p1"}],
                    headers={"X-Pagination-Next": "cursor-2"},
                )
            return httpx.Response(200, json=[{"type": "movie", "ids": {"tmdb": 155}, "play_id": "p2"}])

        with _patched(handler):
            rows = await wetrakr.get_watched_history("tok", "movies", "2026-10-10T00:00:00.000Z")

        self.assertEqual(len(rows), 2)
        self.assertEqual(requests[0].url.params["compact"], "true")
        self.assertEqual(requests[0].url.params["limit"], "5000")
        self.assertEqual(requests[0].url.params["from_date"], "2026-10-10T00:00:00.000Z")
        self.assertNotIn("page", requests[0].url.params)
        self.assertEqual(requests[1].url.params["after"], "cursor-2")

    async def test_ratings_use_from_date_and_compact_mode(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        with _patched(handler):
            await wetrakr.get_ratings("tok", "movies", "2026-10-10T00:00:00.000Z")

        params = captured[0].url.params
        self.assertEqual(params["compact"], "true")
        self.assertEqual(params["limit"], "5000")
        self.assertEqual(params["from_date"], "2026-10-10T00:00:00.000Z")

    async def test_lists_and_comments_use_from_date(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        with _patched(handler):
            await wetrakr.get_lists("tok", "2026-10-10T00:00:00.000Z")
            await wetrakr.get_comments("tok", "movies", "2026-10-10T00:00:00.000Z")

        self.assertEqual(captured[0].url.params["from_date"], "2026-10-10T00:00:00.000Z")
        self.assertEqual(captured[1].url.params["from_date"], "2026-10-10T00:00:00.000Z")

    def test_push_batch_uses_api_maximum(self):
        from routers.wetrakr import WETRAKR_PUSH_BATCH_SIZE
        self.assertEqual(WETRAKR_PUSH_BATCH_SIZE, 5000)


def _reset_wetrakr_state():
    wetrakr._app_quota_blocked_until = 0.0
    wetrakr._user_quota_blocked_until.clear()
    wetrakr._comments_blocked_until = 0.0


class WeTrakrQuotaAndCommentsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_wetrakr_state()
        self.addCleanup(_reset_wetrakr_state)

    async def _send(self, handler, path="/sync/tracking", token="tok"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await wetrakr._send(client, "POST", path, token, json={})

    async def test_app_quota_429_is_not_retried_and_blocks_everyone(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={"error": "APP_QUOTA_EXCEEDED"}, headers={"X-Quota-Reset": "5000"})

        with self.assertRaises(wetrakr.WeTrakrQuotaExceeded):
            await self._send(handler)
        self.assertEqual(len(calls), 1)
        # Another user's request now fails without touching the network.
        with self.assertRaises(wetrakr.WeTrakrQuotaExceeded):
            await self._send(handler, token="someone-else")
        self.assertEqual(len(calls), 1)

    async def test_user_quota_429_blocks_only_that_user(self):
        calls = []

        def handler(request):
            calls.append(request)
            if request.headers["Authorization"] == "Bearer tok":
                return httpx.Response(429, json={"error": "QUOTA_EXCEEDED"})
            return httpx.Response(200, json={})

        with self.assertRaises(wetrakr.WeTrakrQuotaExceeded):
            await self._send(handler)
        with self.assertRaises(wetrakr.WeTrakrQuotaExceeded):
            await self._send(handler)
        self.assertEqual(len(calls), 1)
        resp = await self._send(handler, token="other")
        self.assertEqual(resp.status_code, 200)

    async def test_concurrency_429_is_still_retried(self):
        from unittest import mock
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, json={"error": "CONCURRENCY_LIMIT"}, headers={"Retry-After": "1"})
            return httpx.Response(200, json={})

        async def no_sleep(_):
            return None

        with mock.patch.object(wetrakr.asyncio, "sleep", no_sleep):
            resp = await self._send(handler)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(calls), 2)

    async def test_comments_403_is_remembered_and_skips_further_requests(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(403, json={"error": "COMMENTS_NOT_ENABLED"})

        with _patched(handler):
            with self.assertRaises(wetrakr.WeTrakrCommentsDisabled):
                await wetrakr.get_comments("tok", "movies")
            self.assertTrue(wetrakr.comments_blocked())
            with self.assertRaises(wetrakr.WeTrakrCommentsDisabled):
                await wetrakr.get_comments("tok", "shows")
            with self.assertRaises(wetrakr.WeTrakrCommentsDisabled):
                await wetrakr.write_comment("tok", movie_tmdb_id=603, text="hi")
        self.assertEqual(len(calls), 1)

    async def test_public_lookup_retries_a_concurrency_429(self):
        from unittest import mock
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, json={"error": "CONCURRENCY_LIMIT"}, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"ids": {"tmdb": 603}})

        async def no_sleep(_):
            return None

        with _patched(handler), mock.patch.object(wetrakr.asyncio, "sleep", no_sleep):
            data = await wetrakr.get_movie(126)
        self.assertEqual(data["ids"]["tmdb"], 603)
        self.assertNotIn("Authorization", calls[0].headers)

    async def test_user_locks_do_not_outlive_their_users(self):
        import gc
        lock = wetrakr._user_lock("a-token-that-will-rotate")
        self.assertIs(lock, wetrakr._user_lock("a-token-that-will-rotate"))
        del lock
        gc.collect()
        self.assertNotIn("a-token-that-will-rotate", wetrakr._user_locks)


class WeTrakrPushChunkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_start_at_the_api_maximum(self):
        from routers.wetrakr import _send_in_chunks
        sizes = []

        async def send(chunk):
            sizes.append(len(chunk))

        await _send_in_chunks([("movie", (i, None)) for i in range(12000)], send)
        self.assertEqual(sizes, [5000, 5000, 2000])

    async def test_timeout_resends_the_chunk_in_smaller_batches(self):
        from routers.wetrakr import _send_chunk
        sizes = []

        async def send(chunk):
            sizes.append(len(chunk))
            if len(chunk) > 1000:
                raise httpx.ReadTimeout("slow")

        await _send_chunk(send, [("movie", (i, None)) for i in range(2500)])
        self.assertEqual(sizes, [2500, 1000, 1000, 500])

    async def test_timeout_on_a_small_batch_is_raised(self):
        from routers.wetrakr import _send_chunk

        async def send(chunk):
            raise httpx.ReadTimeout("slow")

        with self.assertRaises(httpx.ReadTimeout):
            await _send_chunk(send, [("movie", (1, None))])

    async def test_non_timeout_errors_are_not_resent(self):
        from routers.wetrakr import _send_chunk
        calls = []

        async def send(chunk):
            calls.append(len(chunk))
            raise wetrakr.WeTrakrQuotaExceeded("quota")

        with self.assertRaises(wetrakr.WeTrakrQuotaExceeded):
            await _send_chunk(send, [("movie", (i, None)) for i in range(3000)])
        self.assertEqual(calls, [3000])

    async def test_writes_use_the_long_timeout(self):
        seen = []
        real = httpx.AsyncClient

        def factory(**kwargs):
            seen.append(kwargs.get("timeout"))
            return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), **kwargs)

        with patch.object(wetrakr.httpx, "AsyncClient", side_effect=factory):
            await wetrakr.add_to_watched_batch("tok", [(1, None)], [])
            await wetrakr.set_ratings_batch("tok", [(1, 8.0)], [])
        self.assertEqual(seen, [wetrakr.WRITE_TIMEOUT, wetrakr.WRITE_TIMEOUT])
        self.assertGreater(wetrakr.WRITE_TIMEOUT, wetrakr.TIMEOUT)


class WeTrakrSectionChangeTests(unittest.TestCase):
    ACTIVITIES = {
        "movies": {"all": "2026-10-10T10:00:00.000Z", "last_tracking_watched_at": "2026-10-10T10:00:00.000Z"},
        "episodes": {"all": "2026-10-01T00:00:00.000Z", "last_tracking_watched_at": "2026-10-01T00:00:00.000Z"},
        "ratings": {"all": "2026-09-01T00:00:00.000Z"},
    }

    def test_only_sections_newer_than_the_stored_stamp_are_read(self):
        from routers.wetrakr import _section_changed
        since = "2026-10-05T00:00:00.000Z"
        self.assertTrue(_section_changed(self.ACTIVITIES, "movies", "last_tracking_watched_at", since))
        self.assertFalse(_section_changed(self.ACTIVITIES, "episodes", "last_tracking_watched_at", since))
        self.assertFalse(_section_changed(self.ACTIVITIES, "ratings", "all", since))

    def test_unknown_state_reads_as_changed(self):
        from routers.wetrakr import _section_changed
        since = "2026-10-05T00:00:00.000Z"
        self.assertTrue(_section_changed(self.ACTIVITIES, "episodes", "last_tracking_watched_at", None))
        self.assertTrue(_section_changed({}, "movies", "last_tracking_watched_at", since))
        self.assertTrue(_section_changed(self.ACTIVITIES, "comments", "all", since))
        self.assertTrue(_section_changed({"movies": {"all": "garbage"}}, "movies", "all", since))
