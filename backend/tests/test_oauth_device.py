import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx
from fastapi import FastAPI
import jwt
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import dependencies
from core.limiter import limiter
from core.security import ALGORITHM, create_access_token, hash_opaque_token
from db import get_db
from dependencies import DEVICE_TOKEN_TYPE, get_current_user
from models.oauth_device import OAuthDeviceGrant
from models.users import User
from routers import auth
from core.timeutil import utcnow

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


def _make_engine():
    return create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


class _DeviceFlowAppMixin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The shared IP-keyed limiter would otherwise trip across tests in this
        # module; the per-endpoint limits themselves aren't what's under test.
        limiter.enabled = False
        self.addCleanup(setattr, limiter, "enabled", True)

        self.engine = _make_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
            await conn.run_sync(OAuthDeviceGrant.__table__.create)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.Session() as s:
            s.add(User(id=1, email="a@b.c", username="alice", api_key="k-alice"))
            s.add(User(id=2, email="d@e.f", username="bob", api_key="k-bob"))
            await s.commit()

        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)
        app.include_router(auth.router, prefix="/auth")

        async def _override_get_db():
            async with self.Session() as session:
                yield session

        app.dependency_overrides[get_db] = _override_get_db
        # First-party (approval / listing) endpoints run as user 1 unless a test
        # swaps this out.
        self.current_user = SimpleNamespace(id=1, is_admin=False)
        app.dependency_overrides[get_current_user] = lambda: self.current_user

        self.app = app
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.engine.dispose)

    async def _grant(self, **overrides):
        async with self.Session() as s:
            res = await s.execute(select(OAuthDeviceGrant))
            grant = res.scalars().first()
            for k, v in overrides.items():
                setattr(grant, k, v)
            await s.commit()
            await s.refresh(grant)
            return grant

    async def _start(self, client_name="Umbrella on Kodi"):
        r = await self.client.post("/auth/device/code", json={"client_name": client_name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class DeviceCodeIssuanceTests(_DeviceFlowAppMixin):
    async def test_issues_device_and_user_code_with_verification_uri(self):
        body = await self._start()
        self.assertIn("device_code", body)
        self.assertRegex(body["user_code"], r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
        self.assertTrue(body["verification_uri"].endswith("/link"))
        self.assertEqual(body["verification_uri_complete"], f"{body['verification_uri']}?code={body['user_code']}")
        self.assertEqual(body["interval"], 5)

        grant = await self._grant()
        self.assertEqual(grant.status, "pending")
        self.assertIsNone(grant.user_id)
        # device_code is never persisted in the clear
        self.assertEqual(grant.device_code_hash, hash_opaque_token(body["device_code"]))
        self.assertNotEqual(grant.device_code_hash, body["device_code"])

    async def test_rejects_unknown_scope(self):
        r = await self.client.post("/auth/device/code", json={"scope": "admin"})
        self.assertEqual(r.status_code, 400)


class DevicePollingStateTests(_DeviceFlowAppMixin):
    async def _poll(self, device_code):
        return await self.client.post(
            "/auth/device/token",
            data={"grant_type": DEVICE_GRANT_TYPE, "device_code": device_code},
        )

    async def test_pending_returns_authorization_pending(self):
        body = await self._start()
        r = await self._poll(body["device_code"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "authorization_pending")

    async def test_polling_faster_than_interval_returns_slow_down_and_bumps_interval(self):
        body = await self._start()
        await self._poll(body["device_code"])
        r = await self._poll(body["device_code"])
        self.assertEqual(r.json()["error"], "slow_down")
        self.assertEqual((await self._grant()).interval, 10)

    async def test_unknown_device_code_is_invalid_grant(self):
        r = await self._poll("nonexistent")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_grant")

    async def test_denied_grant_returns_access_denied(self):
        body = await self._start()
        await self._grant(status="denied", last_polled_at=None)
        r = await self._poll(body["device_code"])
        self.assertEqual(r.json()["error"], "access_denied")

    async def test_expired_grant_returns_expired_token(self):
        body = await self._start()
        await self._grant(expires_at=utcnow() - timedelta(minutes=1), last_polled_at=None)
        r = await self._poll(body["device_code"])
        self.assertEqual(r.json()["error"], "expired_token")


class DeviceApprovalTests(_DeviceFlowAppMixin):
    async def test_pending_lookup_then_approve_then_token_issue(self):
        body = await self._start()
        code = body["user_code"]

        pend = await self.client.get("/auth/device/pending", params={"user_code": code.lower()})
        self.assertEqual(pend.status_code, 200, pend.text)
        self.assertEqual(pend.json()["client_name"], "Umbrella on Kodi")
        self.assertEqual(pend.json()["scope"], "write")

        appr = await self.client.post(
            "/auth/device/approve", json={"user_code": code, "action": "approve"}
        )
        self.assertEqual(appr.status_code, 200, appr.text)

        grant = await self._grant()
        self.assertEqual(grant.status, "approved")
        self.assertEqual(grant.user_id, 1)

        tok = await self.client.post(
            "/auth/device/token",
            data={"grant_type": DEVICE_GRANT_TYPE, "device_code": body["device_code"]},
        )
        self.assertEqual(tok.status_code, 200, tok.text)
        payload = tok.json()
        self.assertEqual(payload["token_type"], "bearer")
        self.assertEqual(payload["scope"], "write")
        self.assertIn("refresh_token", payload)

        claims = jwt.decode(payload["access_token"], os.environ["SECRET_KEY"], algorithms=[ALGORITHM])
        self.assertEqual(claims["type"], DEVICE_TOKEN_TYPE)
        self.assertEqual(claims["scope"], "write")
        self.assertEqual(int(claims["sub"]), 1)
        self.assertEqual(int(claims["jti"]), grant.id)

    async def test_device_code_is_single_use_for_token_issuance(self):
        body = await self._start()
        await self.client.post("/auth/device/approve", json={"user_code": body["user_code"], "action": "approve"})
        first = await self.client.post(
            "/auth/device/token",
            data={"grant_type": DEVICE_GRANT_TYPE, "device_code": body["device_code"]},
        )
        self.assertEqual(first.status_code, 200)
        await self._grant(last_polled_at=None)  # bypass slow_down for the second poll
        second = await self.client.post(
            "/auth/device/token",
            data={"grant_type": DEVICE_GRANT_TYPE, "device_code": body["device_code"]},
        )
        self.assertEqual(second.json()["error"], "invalid_grant")

    async def test_deny_blocks_token_issue(self):
        body = await self._start()
        await self.client.post("/auth/device/approve", json={"user_code": body["user_code"], "action": "deny"})
        await self._grant(last_polled_at=None)
        tok = await self.client.post(
            "/auth/device/token",
            data={"grant_type": DEVICE_GRANT_TYPE, "device_code": body["device_code"]},
        )
        self.assertEqual(tok.json()["error"], "access_denied")

    async def test_pending_lookup_unknown_code_is_404(self):
        r = await self.client.get("/auth/device/pending", params={"user_code": "ZZZZ-9999"})
        self.assertEqual(r.status_code, 404)

    async def test_approval_can_rename_the_connection(self):
        body = await self._start(client_name="Umbrella on Kodi")
        await self.client.post(
            "/auth/device/approve",
            json={"user_code": body["user_code"], "action": "approve", "name": "  Living room TV  "},
        )
        self.assertEqual((await self._grant()).client_name, "Living room TV")

    async def test_blank_name_on_approval_keeps_the_client_supplied_one(self):
        body = await self._start(client_name="Umbrella on Kodi")
        await self.client.post(
            "/auth/device/approve",
            json={"user_code": body["user_code"], "action": "approve", "name": "   "},
        )
        self.assertEqual((await self._grant()).client_name, "Umbrella on Kodi")


class DeviceRefreshTests(_DeviceFlowAppMixin):
    async def _approved_tokens(self):
        body = await self._start()
        await self.client.post("/auth/device/approve", json={"user_code": body["user_code"], "action": "approve"})
        tok = await self.client.post(
            "/auth/device/token",
            data={"grant_type": DEVICE_GRANT_TYPE, "device_code": body["device_code"]},
        )
        return tok.json()

    async def test_refresh_rotates_the_refresh_token(self):
        first = await self._approved_tokens()
        r = await self.client.post(
            "/auth/device/token",
            data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        second = r.json()
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertIn("access_token", second)

    async def test_replaying_a_rotated_refresh_token_revokes_the_grant(self):
        first = await self._approved_tokens()
        await self.client.post(
            "/auth/device/token",
            data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        )
        # The old token is now stale; presenting it again is treated as theft.
        replay = await self.client.post(
            "/auth/device/token",
            data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        )
        self.assertEqual(replay.json()["error"], "invalid_grant")
        self.assertIsNotNone((await self._grant()).revoked_at)


class DeviceGrantManagementTests(_DeviceFlowAppMixin):
    async def _approve_as(self, user_id):
        self.current_user = SimpleNamespace(id=user_id, is_admin=False)
        self.app.dependency_overrides[get_current_user] = lambda: self.current_user
        body = await self._start()
        await self.client.post("/auth/device/approve", json={"user_code": body["user_code"], "action": "approve"})
        return body

    async def test_list_only_shows_current_users_active_grants(self):
        await self._approve_as(1)
        r = await self.client.get("/auth/device/grants")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 1)
        self.assertEqual(r.json()[0]["client_name"], "Umbrella on Kodi")

        self.current_user = SimpleNamespace(id=2, is_admin=False)
        self.app.dependency_overrides[get_current_user] = lambda: self.current_user
        r2 = await self.client.get("/auth/device/grants")
        self.assertEqual(r2.json(), [])

    async def test_revoke_sets_revoked_at_and_blocks_the_access_token(self):
        await self._approve_as(1)
        grant = await self._grant()
        r = await self.client.delete(f"/auth/device/grants/{grant.id}")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone((await self._grant()).revoked_at)

        # A live access token minted from that grant now fails auth.
        token = create_access_token(
            subject=1, expires_delta=timedelta(hours=1),
            extra_claims={"type": DEVICE_TOKEN_TYPE, "scope": "write", "jti": str(grant.id)},
        )
        async with self.Session() as s:
            user = await dependencies.get_optional_user(
                db=s, token=token,
            )
        self.assertIsNone(user)

    async def test_cannot_revoke_another_users_grant(self):
        await self._approve_as(1)
        grant = await self._grant()
        self.current_user = SimpleNamespace(id=2, is_admin=False)
        self.app.dependency_overrides[get_current_user] = lambda: self.current_user
        r = await self.client.delete(f"/auth/device/grants/{grant.id}")
        self.assertEqual(r.status_code, 404)


class DeviceTokenScopeEnforcementTests(_DeviceFlowAppMixin):
    async def test_get_optional_user_accepts_an_approved_device_token(self):
        self.current_user = SimpleNamespace(id=1, is_admin=False)
        body = await self._start()
        await self.client.post("/auth/device/approve", json={"user_code": body["user_code"], "action": "approve"})
        grant = await self._grant()
        token = create_access_token(
            subject=1, expires_delta=timedelta(hours=1),
            extra_claims={"type": DEVICE_TOKEN_TYPE, "scope": "write", "jti": str(grant.id)},
        )
        async with self.Session() as s:
            user = await dependencies.get_optional_user(db=s, token=token)
        self.assertIsNotNone(user)
        self.assertEqual(user.id, 1)

    async def test_get_current_user_rejects_a_device_token_with_403(self):
        token = create_access_token(
            subject=1, expires_delta=timedelta(hours=1),
            extra_claims={"type": DEVICE_TOKEN_TYPE, "scope": "write", "jti": "999"},
        )
        async with self.Session() as s:
            with self.assertRaises(Exception) as ctx:
                await dependencies.get_current_user(db=s, token=token)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    async def test_device_token_for_a_nonexistent_grant_is_rejected(self):
        token = create_access_token(
            subject=1, expires_delta=timedelta(hours=1),
            extra_claims={"type": DEVICE_TOKEN_TYPE, "scope": "write", "jti": "123456"},
        )
        async with self.Session() as s:
            self.assertIsNone(await dependencies.get_optional_user(db=s, token=token))

    async def test_device_token_whose_sub_mismatches_the_grant_owner_is_rejected(self):
        body = await self._start()
        await self.client.post("/auth/device/approve", json={"user_code": body["user_code"], "action": "approve"})
        grant = await self._grant()  # owned by user 1
        # Forged token: valid signature, points at the real grant, but claims sub=2.
        token = create_access_token(
            subject=2, expires_delta=timedelta(hours=1),
            extra_claims={"type": DEVICE_TOKEN_TYPE, "scope": "write", "jti": str(grant.id)},
        )
        async with self.Session() as s:
            self.assertIsNone(await dependencies.get_optional_user(db=s, token=token))

    async def test_grant_listing_never_serializes_secret_hashes(self):
        await self._start()
        body = await self._start()
        await self.client.post("/auth/device/approve", json={"user_code": body["user_code"], "action": "approve"})
        r = await self.client.get("/auth/device/grants")
        self.assertEqual(r.status_code, 200)
        blob = r.text
        for leak in ("device_code_hash", "refresh_token_hash", "prev_refresh_token_hash", "user_id"):
            self.assertNotIn(leak, blob)


class UserCodeHelpersTests(unittest.TestCase):
    def test_normalize_accepts_lenient_input(self):
        self.assertEqual(auth._normalize_user_code("abcd efgh"), "ABCD-EFGH")
        self.assertEqual(auth._normalize_user_code("ABCD-EFGH"), "ABCD-EFGH")

    def test_normalize_rejects_wrong_length_or_bad_chars(self):
        self.assertEqual(auth._normalize_user_code("ABC-DEF"), "")
        self.assertEqual(auth._normalize_user_code(""), "")

    def test_generated_codes_use_the_unambiguous_alphabet(self):
        for _ in range(50):
            code = auth._generate_user_code().replace("-", "")
            self.assertFalse(set(code) & set("O0I1L"))


if __name__ == "__main__":
    unittest.main()
