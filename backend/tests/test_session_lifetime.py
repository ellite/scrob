import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from jose import jwt

from core import security
from core.limiter import limiter
from routers import auth


def _request():
    request = MagicMock()
    request.client.host = "127.0.0.1"
    request.headers = {}
    return request


def _form(username="agent", password="pw"):
    form = MagicMock()
    form.username = username
    form.password = password
    return form


def _db_returning(user):
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _exp_days(token: str) -> float:
    claims = jwt.decode(token, security.settings.secret_key, algorithms=[security.ALGORITHM])
    return (datetime.fromtimestamp(claims["exp"], timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 86400


class SessionLifetimeTests(unittest.TestCase):
    def test_default_is_one_week(self):
        self.assertEqual(security.session_lifetime(False).days, 7)

    def test_remember_uses_the_configured_days(self):
        with patch.object(security.settings, "session_remember_days", 45):
            self.assertEqual(security.session_lifetime(True).days, 45)

    def test_remember_never_shorter_than_a_day(self):
        with patch.object(security.settings, "session_remember_days", 0):
            self.assertEqual(security.session_lifetime(True).days, 1)


class LoginRememberTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        limiter.enabled = False
        self.addCleanup(setattr, limiter, "enabled", True)
        self.user = MagicMock(id=7, password_hash="hash", email_confirmed=True, totp_enabled=False)

    async def _login(self, remember, user=None):
        with patch.object(auth, "verify_password", return_value=True), \
             patch.object(auth.app_settings, "oidc_enabled", False), \
             patch.object(auth.app_settings, "require_email_validation", False), \
             patch.object(security.settings, "session_remember_days", 30):
            return await auth.login(_request(), _form(), remember, _db_returning(user or self.user))

    async def test_without_remember_lasts_a_week(self):
        out = await self._login(False)
        self.assertEqual(out["expires_in"], 7 * 24 * 3600)
        self.assertAlmostEqual(_exp_days(out["access_token"]), 7, delta=0.01)

    async def test_with_remember_lasts_the_configured_days(self):
        out = await self._login(True)
        self.assertEqual(out["expires_in"], 30 * 24 * 3600)
        self.assertAlmostEqual(_exp_days(out["access_token"]), 30, delta=0.01)

    async def test_remember_survives_the_2fa_step(self):
        user = MagicMock(id=7, password_hash="hash", email_confirmed=True, totp_enabled=True)
        out = await self._login(True, user)
        self.assertTrue(out["requires_2fa"])
        claims = jwt.decode(out["temp_token"], security.settings.secret_key, algorithms=[security.ALGORITHM])
        self.assertEqual(claims["type"], "2fa_pending")
        self.assertIs(claims["remember"], True)


class RenewSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = MagicMock(id=7)

    async def _renew(self, token, remember_days=30):
        with patch.object(security.settings, "session_remember_days", remember_days):
            return await auth.renew_session(token=token, current_user=self.user)

    async def test_fresh_token_is_not_reissued(self):
        token = security.create_access_token(subject=7, expires_delta=security.session_lifetime(False))
        out = await self._renew(token)
        self.assertIsNone(out["access_token"])

    async def test_aging_token_gets_a_full_new_lifetime(self):
        token = security.create_access_token(subject=7, expires_delta=timedelta(days=2))
        out = await self._renew(token)
        self.assertEqual(out["expires_in"], 7 * 24 * 3600)
        self.assertAlmostEqual(_exp_days(out["access_token"]), 7, delta=0.01)

    async def test_remember_claim_is_kept_and_uses_the_long_lifetime(self):
        token = security.create_access_token(
            subject=7, expires_delta=timedelta(days=10), extra_claims={"rem": True}
        )
        out = await self._renew(token, remember_days=30)
        self.assertEqual(out["expires_in"], 30 * 24 * 3600)
        claims = jwt.decode(out["access_token"], security.settings.secret_key, algorithms=[security.ALGORITHM])
        self.assertIs(claims["rem"], True)

    async def test_typed_tokens_never_slide(self):
        token = security.create_access_token(
            subject=7, expires_delta=timedelta(minutes=30), extra_claims={"type": "device"}
        )
        out = await self._renew(token)
        self.assertIsNone(out["access_token"])


if __name__ == "__main__":
    unittest.main()
