import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import rpdb
from db import get_db
from dependencies import get_current_user
from models.users import UserSettings
from routers import auth


class RpdbProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_requires_explicit_valid_boolean(self):
        for payload, expected in [({"valid": True}, True), ({"valid": False}, False),
                                  ({"valid": "true"}, False), ({"valid": 1}, False),
                                  (True, False), ({}, False)]:
            with self.subTest(payload=payload):
                client = AsyncMock()
                client.get.return_value = httpx.Response(
                    200, json=payload, request=httpx.Request("GET", "https://example.test")
                )
                with patch.object(rpdb.httpx, "AsyncClient") as factory:
                    factory.return_value.__aenter__.return_value = client
                    self.assertEqual(await rpdb.validate_api_key("key"), expected)

    async def test_provider_failures_and_malformed_json_are_not_valid(self):
        for response in [httpx.Response(401), httpx.Response(503), httpx.Response(200, text="not json")]:
            response.request = httpx.Request("GET", "https://example.test")
            client = AsyncMock()
            client.get.return_value = response
            with patch.object(rpdb.httpx, "AsyncClient") as factory:
                factory.return_value.__aenter__.return_value = client
                self.assertFalse(await rpdb.validate_api_key("key"))
        with patch.object(rpdb.httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__.return_value.get.side_effect = httpx.ReadTimeout("timeout")
            self.assertFalse(await rpdb.validate_api_key("key"))

    async def test_key_cannot_change_provider_host_path_or_query(self):
        seen = []
        def respond(request):
            seen.append(request.url)
            return httpx.Response(200, json={"valid": True})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(rpdb.httpx, "AsyncClient", return_value=client):
            self.assertTrue(await rpdb.validate_api_key("  key/evil?x=#fragment  "))
        self.assertEqual(str(seen[0]), "https://api.ratingposterdb.com/key%2Fevil%3Fx%3D%23fragment/isValid")


class RpdbSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rows = {1: UserSettings(user_id=1, rpdb_api_key="original"),
                     2: UserSettings(user_id=2, rpdb_api_key="other-user")}
        self.user_id = 1
        async def execute(query):
            if query.column_descriptions[0]["entity"] is UserSettings:
                user_id = query.compile().params["user_id_1"]
                value = self.rows.get(user_id)
            else:
                value = None
            return SimpleNamespace(scalar_one_or_none=lambda: value)
        self.db = SimpleNamespace(execute=execute, commit=AsyncMock(), refresh=AsyncMock())
        app = FastAPI()
        app.include_router(auth.router, prefix="/auth")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=self.user_id)
        self.app = app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_changed_key_validates_and_is_scoped_to_current_user(self):
        with patch.object(rpdb, "validate_api_key", AsyncMock(return_value=True)):
            response = await self.client.patch("/auth/settings", json={"rpdb_api_key": "  new-key  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rpdb_api_key"], "new-key")
        self.user_id = 2
        response = await self.client.get("/auth/settings")
        self.assertEqual(response.json()["rpdb_api_key"], "other-user")

    async def test_provider_rejection_preserves_key_and_other_preferences(self):
        with patch.object(rpdb, "validate_api_key", AsyncMock(return_value=False)):
            response = await self.client.patch("/auth/settings", json={"rpdb_api_key": "bad", "blur_explicit": False})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows[1].rpdb_api_key, "original")
        self.assertIsNone(self.rows[1].blur_explicit)
        self.db.commit.assert_not_awaited()

    async def test_unchanged_unrelated_and_clear_work_without_provider(self):
        with patch.object(rpdb, "validate_api_key", AsyncMock(side_effect=AssertionError("must not validate"))):
            for body in ({"rpdb_api_key": " original "}, {"blur_explicit": False}, {"rpdb_api_key": " \t "}):
                response = await self.client.patch("/auth/settings", json=body)
                self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["rpdb_api_key"])
        self.assertFalse(response.json()["blur_explicit"])
        self.assertEqual(self.rows[2].rpdb_api_key, "other-user")

    async def test_overlong_key_rejected_without_mutation(self):
        response = await self.client.patch("/auth/settings", json={"rpdb_api_key": "x" * 256})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.rows[1].rpdb_api_key, "original")

    async def test_key_test_is_no_store_and_does_not_save(self):
        for valid in (True, False):
            with patch.object(rpdb, "validate_api_key", AsyncMock(return_value=valid)):
                response = await self.client.post("/auth/test-rpdb", json={"key": "candidate"})
            self.assertEqual(response.json(), {"success": valid})
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.rows[1].rpdb_api_key, "original")
        self.db.commit.assert_not_awaited()

    async def test_anonymous_requests_cannot_read_save_or_test_keys(self):
        del self.app.dependency_overrides[get_current_user]
        for method, path, body in [("GET", "/auth/settings", None),
                                   ("PATCH", "/auth/settings", {"rpdb_api_key": "key"}),
                                   ("POST", "/auth/test-rpdb", {"key": "key"})]:
            response = await self.client.request(method, path, json=body)
            self.assertIn(response.status_code, (401, 403))
