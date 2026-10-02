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

    async def test_lookup_failure_returns_none_instead_of_raising(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        with patch("routers.wetrakr.wetrakr_client.get_movie", side_effect=Exception("boom")):
            result = await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, {})
        self.assertIsNone(result)

    async def test_caches_repeated_lookups_within_one_run(self):
        from routers.wetrakr import _resolve_wetrakr_comment_target
        caches: dict[str, dict[int, dict]] = {}
        with patch("routers.wetrakr.wetrakr_client.get_movie", return_value={"ids": {"tmdb": 603}}) as mock_get:
            await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, caches)
            await _resolve_wetrakr_comment_target({"target": "movie", "movie": {"id": 126}}, caches)
        mock_get.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
