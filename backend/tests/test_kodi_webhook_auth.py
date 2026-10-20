"""POST /webhooks/kodi accepts either the legacy ?api_key= (so add-ons that
are never updated keep working) or an OAuth device-grant Bearer token (#331).
Auth runs before the empty-body short-circuit, so an empty body is enough to
exercise it without standing up the media tables. The GET /kodi/history and
/kodi/ratings endpoints share the same `get_current_user_or_api_key`
dependency. Parsing/scrobble behaviour itself lives in test_webhooks.py."""

import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from core.security import create_access_token
from db import get_db
from dependencies import DEVICE_TOKEN_TYPE
from models.oauth_device import OAuthDeviceGrant
from models.users import User
from routers import webhooks
from core.timeutil import utcnow


class KodiWebhookAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
            await conn.run_sync(OAuthDeviceGrant.__table__.create)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.Session() as s:
            s.add(User(id=1, email="a@b.c", username="alice", api_key="k-alice"))
            s.add(OAuthDeviceGrant(
                id=7,
                device_code_hash="dch",
                user_code="ABCD-EFGH",
                client_name="Kodi - living room",
                scope="write",
                status="approved",
                interval=5,
                user_id=1,
                expires_at=utcnow() + timedelta(hours=1),
            ))
            await s.commit()

        app = FastAPI()
        app.include_router(webhooks.router, prefix="/webhooks")

        async def _override_get_db():
            async with self.Session() as session:
                yield session

        app.dependency_overrides[get_db] = _override_get_db
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.engine.dispose)

    def _bearer(self, grant_id=7, user_id=1):
        token = create_access_token(
            subject=user_id,
            extra_claims={"type": DEVICE_TOKEN_TYPE, "scope": "write", "jti": str(grant_id)},
        )
        return {"Authorization": f"Bearer {token}"}

    async def test_api_key_query_param_is_accepted(self):
        r = await self.client.post("/webhooks/kodi?api_key=k-alice", content=b"")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"status": "ignored", "reason": "empty body"})

    async def test_api_key_header_is_accepted(self):
        r = await self.client.post("/webhooks/kodi", content=b"", headers={"X-Api-Key": "k-alice"})
        self.assertEqual(r.status_code, 200, r.text)

    async def test_device_bearer_token_is_accepted(self):
        r = await self.client.post("/webhooks/kodi", content=b"", headers=self._bearer())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reason"], "empty body")

    async def test_no_credentials_is_401(self):
        r = await self.client.post("/webhooks/kodi", content=b"")
        self.assertEqual(r.status_code, 401)

    async def test_bad_api_key_is_401(self):
        r = await self.client.post("/webhooks/kodi?api_key=nope", content=b"")
        self.assertEqual(r.status_code, 401)

    async def test_revoked_grant_is_401(self):
        async with self.Session() as s:
            grant = await s.get(OAuthDeviceGrant, 7)
            grant.revoked_at = utcnow()
            await s.commit()
        r = await self.client.post("/webhooks/kodi", content=b"", headers=self._bearer())
        self.assertEqual(r.status_code, 401)

    async def test_bearer_token_for_unknown_grant_is_401(self):
        r = await self.client.post("/webhooks/kodi", content=b"", headers=self._bearer(grant_id=999))
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
