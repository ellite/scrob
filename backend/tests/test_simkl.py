import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx

from core import simkl
from core.simkl import SimklHistoryRejected, _history_not_found
from routers.simkl import _simkl_rating_value

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class SimklRatingValueTests(unittest.TestCase):
    """Regression tests for issue #112: Simkl's /sync/ratings response uses
    "user_rating" (most entries None, since it returns every item the user
    has, not just rated ones) - reading the wrong "rating" key made every
    entry look unrated and nothing ever imported."""

    def test_reads_user_rating_field(self):
        item = {"user_rating": 8, "status": "completed", "movie": {"title": "Fight Club"}}
        self.assertEqual(_simkl_rating_value(item), 8.0)

    def test_unrated_item_returns_none(self):
        item = {"user_rating": None, "status": "completed", "movie": {"title": "Fight Club"}}
        self.assertIsNone(_simkl_rating_value(item))

    def test_missing_field_returns_none(self):
        item = {"status": "completed", "movie": {"title": "Fight Club"}}
        self.assertIsNone(_simkl_rating_value(item))

    def test_generic_rating_key_is_ignored(self):
        """The bug itself: a stray "rating" key (used by Simkl's other,
        single-item rate endpoints) must not be mistaken for user_rating."""
        item = {"rating": 9, "user_rating": None}
        self.assertIsNone(_simkl_rating_value(item))

    def test_zero_rating_is_treated_as_unrated(self):
        # Simkl ratings are 1-10; a literal 0 isn't a real rating value.
        item = {"user_rating": 0}
        self.assertIsNone(_simkl_rating_value(item))


class HistoryNotFoundParsingTests(unittest.TestCase):
    def test_dict_of_lists_shape(self):
        payload = {"not_found": {"movies": [], "shows": [{"ids": {"tmdb": 95479}}], "episodes": []}}
        self.assertEqual(_history_not_found(payload), [{"ids": {"tmdb": 95479}}])

    def test_bare_list_shape(self):
        payload = {"not_found": [{"ids": {"tmdb": 1}}]}
        self.assertEqual(_history_not_found(payload), [{"ids": {"tmdb": 1}}])

    def test_empty_not_found(self):
        self.assertEqual(_history_not_found({"added": {"episodes": 1}, "not_found": {"shows": []}}), [])

    def test_non_dict_payload(self):
        self.assertEqual(_history_not_found(None), [])


class AddToHistoryRejectionTests(unittest.IsolatedAsyncioTestCase):
    """#328: Simkl reports a season-layout mismatch (absolute-ordered anime past
    the first cour) as `not_found` *inside* a 201, so raise_for_status() alone
    treats the lost watch as success. The single-item history helpers must
    surface it."""

    def _patched(self, handler):
        transport = httpx.MockTransport(handler)
        return patch.object(
            simkl.httpx, "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        )

    async def test_episode_rejected_when_returned_in_not_found(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={
                "added": {"episodes": 0},
                "not_found": {"shows": [{"ids": {"tmdb": 95479}, "seasons": [{"number": 1}]}]},
            })

        with self._patched(handler):
            with self.assertRaises(SimklHistoryRejected):
                await simkl.add_episode_to_history("cid", "tok", 95479, 1, 25)

    async def test_episode_accepted_when_added(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={
                "added": {"episodes": 1}, "not_found": {"shows": [], "episodes": []},
            })

        with self._patched(handler):
            await simkl.add_episode_to_history("cid", "tok", 95479, 2, 1)  # no raise

    async def test_movie_rejected_when_returned_in_not_found(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={
                "added": {"movies": 0}, "not_found": {"movies": [{"ids": {"tmdb": 999999}}]},
            })

        with self._patched(handler):
            with self.assertRaises(SimklHistoryRejected):
                await simkl.add_movie_to_history("cid", "tok", 999999)

    async def test_batch_logs_but_does_not_raise_on_partial_not_found(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={
                "added": {"episodes": 1},
                "not_found": {"shows": [{"ids": {"tmdb": 95479}}]},
            })

        with self._patched(handler):
            with self.assertLogs("core.simkl", level="WARNING") as logs:
                await simkl.add_history_batch("cid", "tok", [], [(95479, 1, 25, None), (95479, 2, 1, None)])
        self.assertTrue(any("could not resolve" in m for m in logs.output))

    async def test_batch_returns_number_of_unresolved_episodes(self):
        """#453: the push counts these as failures, not successes."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={
                "added": {"episodes": 1},
                "not_found": {"shows": [{"ids": {"tmdb": 46298}, "seasons": [
                    {"number": 2, "episodes": [{"number": 79}, {"number": 80}, {"number": 81}]},
                ]}]},
            })

        with self._patched(handler):
            rejected = await simkl.add_history_batch("cid", "tok", [], [(46298, 2, 79, None)])
        self.assertEqual(rejected, 3)

    async def test_batch_returns_zero_when_everything_resolves(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"added": {"episodes": 1}, "not_found": {}})

        with self._patched(handler):
            self.assertEqual(await simkl.add_history_batch("cid", "tok", [], [(1, 1, 1, None)]), 0)


class SimklAuthV2ClientTests(unittest.IsolatedAsyncioTestCase):
    """#455: AUTH V2 device flow + refresh, alongside the untouched V1 PIN flow."""

    def _patched(self, handler):
        transport = httpx.MockTransport(handler)
        return patch.object(
            simkl.httpx, "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        )

    async def test_v1_client_id_raises_not_v2(self):
        def handler(request):
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "not enabled for OAuth 2.0"})

        with self._patched(handler):
            with self.assertRaises(simkl.SimklNotV2Client):
                await simkl.start_device_auth_v2("v1-id")

    async def test_start_requests_write_scope(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-EFGH"})

        with self._patched(handler):
            out = await simkl.start_device_auth_v2("cid")
        self.assertEqual(seen["path"], "/oauth2/device")
        self.assertIn("media%3Awrite", seen["body"])
        self.assertEqual(out["user_code"], "ABCD-EFGH")

    async def test_poll_pending_slow_down_and_expired(self):
        for error, expected in (("authorization_pending", None), ("slow_down", None)):
            with self._patched(lambda r, e=error: httpx.Response(400, json={"error": e})):
                self.assertIsNone(await simkl.poll_device_token_v2("cid", "dc"))
        with self._patched(lambda r: httpx.Response(400, json={"error": "expired_token"})):
            with self.assertRaises(simkl.SimklAuthError):
                await simkl.poll_device_token_v2("cid", "dc")

    async def test_poll_returns_tokens_when_approved(self):
        tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 604800, "scope": "media:read media:write"}
        with self._patched(lambda r: httpx.Response(200, json=tokens)):
            self.assertEqual(await simkl.poll_device_token_v2("cid", "dc"), tokens)

    async def test_refresh_success_and_rejection(self):
        with self._patched(lambda r: httpx.Response(200, json={"access_token": "new", "refresh_token": "r", "expires_in": 604800})):
            self.assertEqual((await simkl.refresh_access_token_v2("cid", "r"))["access_token"], "new")
        with self._patched(lambda r: httpx.Response(400, json={"error": "invalid_grant"})):
            with self.assertRaises(simkl.SimklAuthError):
                await simkl.refresh_access_token_v2("cid", "r")
        with self._patched(lambda r: httpx.Response(500, json={})):
            with self.assertRaises(httpx.HTTPStatusError):
                await simkl.refresh_access_token_v2("cid", "r")

    def test_needs_refresh_rules(self):
        from types import SimpleNamespace
        now = 1_000_000
        v1 = SimpleNamespace(simkl_client_id="c", simkl_refresh_token=None, simkl_token_expires_at=None)
        fresh = SimpleNamespace(simkl_client_id="c", simkl_refresh_token="r", simkl_token_expires_at=now + 6 * 86400)
        soon = SimpleNamespace(simkl_client_id="c", simkl_refresh_token="r", simkl_token_expires_at=now + 86400)
        expired = SimpleNamespace(simkl_client_id="c", simkl_refresh_token="r", simkl_token_expires_at=now - 10)
        unknown = SimpleNamespace(simkl_client_id="c", simkl_refresh_token="r", simkl_token_expires_at=None)
        self.assertFalse(simkl.needs_refresh(v1, now))      # V1 tokens are never refreshed
        self.assertFalse(simkl.needs_refresh(fresh, now))
        self.assertTrue(simkl.needs_refresh(soon, now))
        self.assertTrue(simkl.needs_refresh(expired, now))
        self.assertTrue(simkl.needs_refresh(unknown, now))


class SimklAuthV2RouterTests(unittest.IsolatedAsyncioTestCase):
    def _db(self, settings):
        from unittest.mock import AsyncMock, MagicMock
        db = AsyncMock()
        res = MagicMock()
        res.scalar_one_or_none.return_value = settings
        db.execute = AsyncMock(return_value=res)
        return db

    def _settings(self, **kw):
        from types import SimpleNamespace
        base = dict(
            user_id=1, simkl_client_id="cid", simkl_access_token=None, simkl_refresh_token=None,
            simkl_token_expires_at=None, simkl_device_code=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    async def test_start_uses_v2_when_client_is_v2(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        import routers.simkl as r
        s = self._settings()
        v2 = {"device_code": "dc", "user_code": "ABCD-EFGH", "verification_uri_complete": "https://simkl.com/pin?user_code=ABCD-EFGH", "expires_in": 900, "interval": 5}
        with patch.object(r.simkl_client, "start_device_auth_v2", AsyncMock(return_value=v2)), \
             patch.object(r.simkl_client, "start_pin_auth", AsyncMock()) as v1:
            out = await r.simkl_pin_start(db=self._db(s), current_user=SimpleNamespace(id=1))
        v1.assert_not_awaited()
        self.assertEqual(s.simkl_device_code, "v2:dc")
        self.assertEqual(out["user_code"], "ABCD-EFGH")
        self.assertIn("user_code=ABCD-EFGH", out["url"])

    async def test_start_without_a_client_id_uses_scrobs_own_app(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        import routers.simkl as r
        s = self._settings(simkl_client_id=None)
        v2 = {"device_code": "dc", "user_code": "ABCD-EFGH"}
        with patch.object(r.simkl_client, "start_device_auth_v2", AsyncMock(return_value=v2)) as start:
            await r.simkl_pin_start(db=self._db(s), current_user=SimpleNamespace(id=1))
        start.assert_awaited_once_with(simkl.SCROB_CLIENT_ID)
        self.assertEqual(s.simkl_client_id, simkl.SCROB_CLIENT_ID)

    async def test_reconnect_replaces_a_saved_v1_client_id_with_scrobs_v2_app(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        import routers.simkl as r
        s = self._settings(simkl_client_id="old-v1-id")
        with patch.object(r.simkl_client, "start_device_auth_v2",
                          AsyncMock(return_value={"device_code": "dc", "user_code": "ABCD-EFGH"})) as start:
            out = await r.simkl_pin_start(db=self._db(s), current_user=SimpleNamespace(id=1))
        start.assert_awaited_once_with(simkl.SCROB_CLIENT_ID)
        self.assertEqual(s.simkl_client_id, simkl.SCROB_CLIENT_ID)
        self.assertEqual(s.simkl_device_code, "v2:dc")
        self.assertEqual(out["user_code"], "ABCD-EFGH")

    async def test_poll_v2_stores_tokens_and_expiry(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        import routers.simkl as r
        s = self._settings(simkl_device_code="v2:dc")
        tokens = {"access_token": "a", "refresh_token": "rt", "expires_in": 604800, "scope": "media:read media:write"}
        with patch.object(r.simkl_client, "poll_device_token_v2", AsyncMock(return_value=tokens)) as poll:
            out = await r.simkl_pin_poll(db=self._db(s), current_user=SimpleNamespace(id=1))
        poll.assert_awaited_once_with("cid", "dc")
        self.assertEqual(out, {"status": "connected"})
        self.assertEqual((s.simkl_access_token, s.simkl_refresh_token), ("a", "rt"))
        self.assertGreater(s.simkl_token_expires_at, 0)
        self.assertIsNone(s.simkl_device_code)

    async def test_poll_v1_pin_leaves_no_refresh_state(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        import routers.simkl as r
        s = self._settings(simkl_device_code="12345", simkl_refresh_token="stale", simkl_token_expires_at=5)
        with patch.object(r.simkl_client, "poll_pin_token", AsyncMock(return_value="v1tok")):
            out = await r.simkl_pin_poll(db=self._db(s), current_user=SimpleNamespace(id=1))
        self.assertEqual(out, {"status": "connected"})
        self.assertEqual(s.simkl_access_token, "v1tok")
        self.assertIsNone(s.simkl_refresh_token)
        self.assertIsNone(s.simkl_token_expires_at)

    async def test_ensure_fresh_skips_v1_and_refreshes_expiring_v2(self):
        from unittest.mock import AsyncMock
        import routers.simkl as r
        db = self._db(None)
        v1 = self._settings(simkl_access_token="v1tok")
        with patch.object(r.simkl_client, "refresh_access_token_v2", AsyncMock()) as refresh:
            self.assertTrue(await r.ensure_simkl_token_fresh(db, v1))
        refresh.assert_not_awaited()

        v2 = self._settings(simkl_access_token="old", simkl_refresh_token="rt", simkl_token_expires_at=1)
        with patch.object(r.simkl_client, "refresh_access_token_v2",
                          AsyncMock(return_value={"access_token": "new", "refresh_token": "rt", "expires_in": 604800})):
            self.assertTrue(await r.ensure_simkl_token_fresh(db, v2))
        self.assertEqual(v2.simkl_access_token, "new")
        self.assertGreater(v2.simkl_token_expires_at, 1)

    async def test_ensure_fresh_reports_failure_without_clearing_tokens(self):
        from unittest.mock import AsyncMock
        import routers.simkl as r
        v2 = self._settings(simkl_access_token="old", simkl_refresh_token="rt", simkl_token_expires_at=1)
        with patch.object(r.simkl_client, "refresh_access_token_v2", AsyncMock(side_effect=simkl.SimklAuthError("revoked"))):
            self.assertFalse(await r.ensure_simkl_token_fresh(self._db(None), v2))
        self.assertEqual(v2.simkl_access_token, "old")


if __name__ == "__main__":
    unittest.main()
