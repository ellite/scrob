"""The Jellyfin and Kodi webhooks accept an optional top-level "source" so an
API client posting their shape can name itself (#504). Validation runs before
parsing, so an invalid source is rejected without standing up the media
tables, and a valid one falls through to the normal parse."""

import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from db import get_db
from models.users import User
from routers import webhooks
from routers.webhooks import _webhook_source


class WebhookSourceTests(unittest.TestCase):
    def test_missing_source_uses_the_default(self):
        self.assertEqual(_webhook_source({}, "jellyfin"), "jellyfin")

    def test_valid_source_is_used(self):
        self.assertEqual(_webhook_source({"source": "silo"}, "jellyfin"), "silo")

    def test_invalid_sources_are_rejected(self):
        for bad in ("Silo", "", "x" * 17, "si lo", 5, None):
            with self.subTest(source=bad):
                self.assertIsNone(_webhook_source({"source": bad}, "jellyfin"))


class WebhookSourceEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.Session() as s:
            s.add(User(id=1, email="a@b.c", username="alice", api_key="k-alice"))
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

    async def test_jellyfin_rejects_an_invalid_source(self):
        r = await self.client.post(
            "/webhooks/jellyfin?api_key=k-alice", json={"source": "Silo", "NotificationType": "PlaybackStart"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "error")

    async def test_jellyfin_accepts_a_valid_source(self):
        # An unknown notification type is ignored by the parser, which shows
        # the valid source got past validation.
        r = await self.client.post(
            "/webhooks/jellyfin?api_key=k-alice", json={"source": "silo", "NotificationType": "Unknown"}
        )
        self.assertEqual(r.json(), {"status": "ignored"})

    async def test_kodi_rejects_an_invalid_source(self):
        r = await self.client.post("/webhooks/kodi?api_key=k-alice", json={"source": "Kodi Box"})
        self.assertEqual(r.json()["status"], "error")


if __name__ == "__main__":
    unittest.main()
