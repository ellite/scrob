import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from fastapi import HTTPException

from core.limiter import limiter
from routers import auth
import schemas


def _request():
    request = MagicMock()
    request.client.host = "127.0.0.1"
    request.headers = {}
    return request


def _db(email_in_use=False):
    result = MagicMock()
    result.first.return_value = (99,) if email_in_use else None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


def _user(**kw):
    return SimpleNamespace(id=7, email="old@example.com", password_hash="hash", email_confirmed=True, **kw)


class ChangeEmailTests(unittest.IsolatedAsyncioTestCase):
    """#471: the web UI had no way to change an account's email."""

    def setUp(self):
        limiter.enabled = False
        self.addCleanup(setattr, limiter, "enabled", True)

    async def _change(self, user, new_email="new@example.com", password="pw", *, validation=False, in_use=False, verified=True):
        db = _db(in_use)
        body = schemas.EmailChange(new_email=new_email, current_password=password)
        with patch.object(auth, "verify_password", return_value=verified), \
             patch.object(auth.app_settings, "require_email_validation", validation), \
             patch.object(auth, "send_email_change_email", new_callable=AsyncMock) as send:
            out = await auth.change_email(_request(), body, db, user)
        return out, db, send

    async def test_applies_immediately_without_email_validation(self):
        user = _user()
        out, db, send = await self._change(user)
        self.assertEqual(user.email, "new@example.com")
        self.assertEqual(out["status"], "email updated")
        send.assert_not_awaited()

    async def test_with_validation_keeps_old_email_and_sends_a_link(self):
        user = _user()
        out, db, send = await self._change(user, validation=True)
        self.assertEqual(user.email, "old@example.com")
        self.assertEqual(out["status"], "confirmation sent")
        activation = db.add.call_args.args[0]
        self.assertEqual((activation.user_id, activation.email), (7, "new@example.com"))
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], "new@example.com")

    async def test_wrong_password_is_rejected(self):
        user = _user()
        with self.assertRaises(HTTPException) as ctx:
            await self._change(user, verified=False)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(user.email, "old@example.com")

    async def test_missing_password_is_rejected_when_the_account_has_one(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._change(_user(), password=None)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_passwordless_oidc_account_can_change_without_a_password(self):
        user = _user()
        user.password_hash = None
        out, _db_, _send = await self._change(user, password=None)
        self.assertEqual(user.email, "new@example.com")

    async def test_taken_address_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._change(_user(), in_use=True)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_same_address_in_any_case_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._change(_user(), new_email="OLD@example.com")
        self.assertEqual(ctx.exception.status_code, 400)


class ApplyActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_link_just_confirms(self):
        user = _user()
        user.email_confirmed = False
        ok = await auth._apply_activation(_db(), user, SimpleNamespace(email="old@example.com"))
        self.assertTrue(ok)
        self.assertTrue(user.email_confirmed)
        self.assertEqual(user.email, "old@example.com")

    async def test_change_link_switches_the_address(self):
        user = _user()
        ok = await auth._apply_activation(_db(), user, SimpleNamespace(email="new@example.com"))
        self.assertTrue(ok)
        self.assertEqual(user.email, "new@example.com")

    async def test_change_link_for_an_address_taken_meanwhile_is_refused(self):
        user = _user()
        ok = await auth._apply_activation(_db(email_in_use=True), user, SimpleNamespace(email="new@example.com"))
        self.assertFalse(ok)
        self.assertEqual(user.email, "old@example.com")
