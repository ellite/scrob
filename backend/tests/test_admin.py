import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx
from fastapi import FastAPI, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from db import get_db
from dependencies import require_admin
from routers import admin
import schemas


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Minimal AsyncSession stand-in for admin_heal_metadata: one settings
    lookup, then add/commit/refresh of the SyncJob."""

    def __init__(self):
        self.added = []
        self.info = {}

    async def execute(self, _stmt):
        return _Result(None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        obj.id = 123


class AdminHealTmdbKeyResolutionTests(unittest.IsolatedAsyncioTestCase):
    """#336: the server-wide heal must resolve the TMDB key the same way every
    other TMDB path does - this admin's own key, then the global one - not
    require a global key specifically."""

    async def asyncSetUp(self):
        self.db = _FakeDB()
        app = FastAPI()
        app.include_router(admin.router, prefix="/admin")

        async def _override_db():
            yield self.db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, is_admin=True)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_starts_with_a_per_user_key_when_no_global_key(self):
        with (
            patch("routers.sync._get_effective_tmdb_key", AsyncMock(return_value="user-key")),
            patch("routers.admin.run_admin_heal", AsyncMock()),
        ):
            res = await self.client.post("/admin/maintenance/heal")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "started")

    async def test_passes_the_resolved_key_to_the_background_task(self):
        captured = {}

        def _capture(*args, **kwargs):
            captured["key"] = args[0]

        with (
            patch("routers.sync._get_effective_tmdb_key", AsyncMock(return_value="resolved-key")),
            patch("routers.admin.run_admin_heal", side_effect=_capture),
        ):
            await self.client.post("/admin/maintenance/heal")
        self.assertEqual(captured["key"], "resolved-key")

    async def test_400_only_when_no_key_at_all(self):
        with patch("routers.sync._get_effective_tmdb_key", AsyncMock(return_value=None)):
            res = await self.client.post("/admin/maintenance/heal")
        self.assertEqual(res.status_code, 400)
        detail = res.json()["detail"]
        self.assertIn("TMDB", detail)
        self.assertIn("Settings", detail)  # points the user somewhere


class AdminGlobalApiKeyConnectionTestTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_valid_tmdb_key(self):
        response = Response()
        with patch("core.tmdb.validate_api_key", AsyncMock(return_value=True)) as validate:
            result = await admin.test_global_tmdb(
                schemas.ApiKeyTestRequest(key="tmdb-key"),
                response,
                SimpleNamespace(id=1, is_admin=True),
            )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate.assert_awaited_once_with("tmdb-key")

    async def test_tvdb_key_includes_optional_subscriber_pin(self):
        response = Response()
        with patch("core.tvdb.validate_api_key", AsyncMock(return_value=True)) as validate:
            result = await admin.test_global_tvdb(
                schemas.ApiKeyTestRequest(key="tvdb-key", pin="  subscriber-pin  "),
                response,
                SimpleNamespace(id=1, is_admin=True),
            )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate.assert_awaited_once_with("tvdb-key", pin="subscriber-pin")

    async def test_accepts_valid_mdblist_key_without_saving_it(self):
        response = Response()
        with patch("core.mdblist.validate_api_key", AsyncMock(return_value=True)) as validate:
            result = await admin.test_global_mdblist(
                schemas.ApiKeyTestRequest(key="  valid-key  "),
                response,
                SimpleNamespace(id=1, is_admin=True),
            )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        validate.assert_awaited_once_with("valid-key")

    async def test_rejects_invalid_mdblist_key(self):
        with patch("core.mdblist.validate_api_key", AsyncMock(return_value=False)):
            with self.assertRaises(HTTPException) as raised:
                await admin.test_global_mdblist(
                    schemas.ApiKeyTestRequest(key="invalid-key"),
                    Response(),
                    SimpleNamespace(id=1, is_admin=True),
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Failed to connect to MDBList")


class AdminCreateUserTests(unittest.IsolatedAsyncioTestCase):
    """POST /admin/users manually provisions an account, bypassing the
    registration gate and marking the email confirmed."""

    async def asyncSetUp(self):
        from models.users import User

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.User = User

        app = FastAPI()
        app.include_router(admin.router, prefix="/admin")

        async def _override_db():
            async with self.Session() as s:
                yield s

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, is_admin=True)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.engine.dispose)

    async def _get_user(self, username):
        async with self.Session() as s:
            return (await s.execute(select(self.User).where(self.User.username == username))).scalar_one_or_none()

    async def test_creates_a_regular_user_ready_to_sign_in(self):
        res = await self.client.post("/admin/users", json={
            "username": "  Neo  ", "email": "NEO@example.com ", "password": "redpill",
        })
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        self.assertEqual(body["username"], "Neo")
        self.assertEqual(body["email"], "neo@example.com")
        self.assertFalse(body["is_admin"])
        self.assertTrue(body["api_key"])

        u = await self._get_user("Neo")
        self.assertTrue(u.email_confirmed)
        self.assertNotEqual(u.password_hash, "redpill")
        from core.security import verify_password
        self.assertTrue(verify_password("redpill", u.password_hash))

    async def test_is_admin_flag_sets_both_is_admin_and_the_admin_role(self):
        res = await self.client.post("/admin/users", json={
            "username": "trinity", "email": "trinity@example.com", "password": "x", "is_admin": True,
        })
        self.assertEqual(res.status_code, 201, res.text)
        u = await self._get_user("trinity")
        self.assertTrue(u.is_admin)
        self.assertEqual(u.role.value if hasattr(u.role, "value") else u.role, "admin")

    async def test_duplicate_username_or_email_is_rejected(self):
        await self.client.post("/admin/users", json={
            "username": "dup", "email": "dup@example.com", "password": "x",
        })
        same_email = await self.client.post("/admin/users", json={
            "username": "other", "email": "DUP@example.com", "password": "x",
        })
        self.assertEqual(same_email.status_code, 400)
        same_name = await self.client.post("/admin/users", json={
            "username": "dup", "email": "new@example.com", "password": "x",
        })
        self.assertEqual(same_name.status_code, 400)

    async def test_bad_email_is_a_422(self):
        res = await self.client.post("/admin/users", json={
            "username": "x", "email": "not-an-email", "password": "x",
        })
        self.assertEqual(res.status_code, 422)


if __name__ == "__main__":
    unittest.main()
