import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from starlette.requests import Request

from core import oidc as core_oidc
from core.limiter import limiter
from routers import oidc


# --------------------------------------------------------------------------- #
# Shared fakes.  Mirrors the hand-rolled mocking pattern already in use across
# tests/test_shows.py / test_history.py / the previous version of this file -
# a real DB would need aiosqlite, which isn't a declared dep here.
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json_data = json_data

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json_data


class _FakeHttpClient:
    def __init__(self, token_response, userinfo_response):
        self._token_response = token_response
        self._userinfo_response = userinfo_response
        self.last_token_body = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.last_token_body = kwargs.get("data")
        return self._token_response

    async def get(self, url, **kwargs):
        return self._userinfo_response


class _Result:
    """Generic scalar_one_or_none / scalar_one / scalars.all stand-in."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]


class _FakeSession:
    """Queues results for successive db.execute() calls."""

    def __init__(self, results):
        self.added = []
        self.deleted = []
        self.execute = AsyncMock(side_effect=[_Result(r) if not isinstance(r, _Result) else r for r in results])
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.refresh = AsyncMock()
        self.flush = AsyncMock(side_effect=self._on_flush)

        def _add(obj):
            # Assign a pk so subsequent code paths that read .id work.
            if getattr(obj, "id", None) is None:
                obj.id = len(self.added) + 100
            self.added.append(obj)
        self.add = MagicMock(side_effect=_add)
        self.delete = AsyncMock(side_effect=self.deleted.append)

    async def _on_flush(self):
        return None


def _make_request():
    """A minimal Starlette Request so the slowapi decorator doesn't blow up.
    Rate limiting itself is disabled per-test via ``limiter.enabled = False``."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


# --------------------------------------------------------------------------- #


class _OidcTestBase(unittest.IsolatedAsyncioTestCase):
    """Common settings + limiter + id-token-verification stubs."""

    #: subclasses may override to switch between compat and hardened mode.
    hardened = True

    def setUp(self) -> None:
        super().setUp()
        limiter.enabled = False
        self.addCleanup(setattr, limiter, "enabled", True)

        # Reset the one-shot compat warning so tests are order-independent.
        oidc._compat_warned = False

        self._settings_patch = patch.multiple(
            oidc.app_settings,
            oidc_enabled=True,
            oidc_auto_create_users=True,
            oidc_identifier_field="email",
            oidc_token_url="https://provider.example/token",
            oidc_userinfo_url="https://provider.example/userinfo",
            oidc_auth_url="https://provider.example/authorize",
            oidc_redirect_url="https://scrob.example/oidc-callback",
            oidc_client_id="client-id",
            oidc_client_secret="client-secret",
            oidc_scopes="openid email profile",
            oidc_pkce=True,
            oidc_provider_name="TestIdP",
            oidc_disable_password_login=False,
            oidc_discovery_url="https://provider.example/.well-known/openid-configuration" if self.hardened else None,
            oidc_issuer=None,
            oidc_jwks_url=None,
            oidc_logout_url=None,
            oidc_post_logout_redirect_url=None,
            server_url="https://scrob.example",
        )
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)

        self._avail_patch = patch("routers.oidc.id_token_validation_available", return_value=self.hardened)
        self._avail_patch.start()
        self.addCleanup(self._avail_patch.stop)

        self._issuer_patch = patch("routers.oidc.effective_issuer", AsyncMock(return_value="https://provider.example"))
        self._issuer_patch.start()
        self.addCleanup(self._issuer_patch.stop)

    @staticmethod
    def _patch_verify(claims):
        """Patches routers.oidc.verify_id_token to return the given claims."""
        return patch("routers.oidc.verify_id_token", AsyncMock(return_value=claims))

    @staticmethod
    def _patch_verify_raises(exc):
        return patch("routers.oidc.verify_id_token", AsyncMock(side_effect=exc))


# ============================================================================
# /exchange - hardened path (id_token validated, (issuer, sub) binding)
# ============================================================================


class HardenedExchangeTests(_OidcTestBase):
    hardened = True

    async def test_remember_issues_a_longer_lived_session(self):
        claims = {"iss": "https://provider.example", "sub": "u1", "email": "new@example.com", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u1", "email": "new@example.com", "email_verified": True})
        db = _FakeSession([None, None, None, 0])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt") as make_token, \
             patch("core.security.settings.session_remember_days", 30):
            resp = await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n", remember=True), db)

        self.assertEqual(resp["expires_in"], 30 * 24 * 3600)
        self.assertEqual(make_token.call_args.kwargs["expires_delta"].days, 30)

    async def test_first_oidc_user_becomes_admin(self):
        """Regression for #159: first user auto-created via OIDC is admin."""
        claims = {"iss": "https://provider.example", "sub": "u1", "email": "new@example.com", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u1", "email": "new@example.com", "email_verified": True})

        # execute order: identity lookup (none) → email lookup (none) →
        # username lookup (none) → count(*) (0).
        db = _FakeSession([None, None, None, 0])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            resp = await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertEqual(resp, {"access_token": "jwt", "expires_in": 7 * 24 * 3600})
        user, identity = db.added  # new user + its oidc_identity
        self.assertTrue(user.is_admin)
        self.assertEqual(identity.issuer, "https://provider.example")
        self.assertEqual(identity.subject, "u1")

    async def test_subsequent_oidc_user_is_not_admin(self):
        claims = {"iss": "https://provider.example", "sub": "u2", "email": "b@example.com", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u2", "email": "b@example.com"})
        db = _FakeSession([None, None, None, 1])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertFalse(db.added[0].is_admin)

    async def test_returning_user_matched_by_issuer_sub(self):
        """Second login for a known (issuer, sub) must not auto-create a new user."""
        existing_user = MagicMock(id=7, email="old@example.com")
        existing_identity = MagicMock(user_id=7, issuer="https://provider.example", subject="u3")

        claims = {"iss": "https://provider.example", "sub": "u3", "email": "old@example.com", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u3", "email": "old@example.com"})
        # execute: identity found → user lookup returns the user.
        db = _FakeSession([existing_identity, existing_user])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt-7"):
            await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertEqual(db.added, [])  # no new user, no new identity

    async def test_email_change_updates_user_when_verified(self):
        existing_user = MagicMock(id=7, email="old@example.com")
        existing_identity = MagicMock(user_id=7, issuer="https://provider.example", subject="u3")
        claims = {"iss": "https://provider.example", "sub": "u3", "email": "new@example.com", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u3", "email": "new@example.com"})
        # execute: identity → user → collision check (no clash).
        db = _FakeSession([existing_identity, existing_user, None])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertEqual(existing_user.email, "new@example.com")

    async def test_email_change_ignored_when_unverified(self):
        existing_user = MagicMock(id=7, email="old@example.com")
        existing_identity = MagicMock(user_id=7, issuer="https://provider.example", subject="u3")
        claims = {"iss": "https://provider.example", "sub": "u3", "email": "new@example.com", "email_verified": False}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u3", "email": "new@example.com", "email_verified": False})
        db = _FakeSession([existing_identity, existing_user])  # no collision check expected

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertEqual(existing_user.email, "old@example.com")

    async def test_email_change_skipped_on_collision(self):
        existing_user = MagicMock(id=7, email="old@example.com")
        clashing_user = MagicMock(id=9, email="new@example.com")
        existing_identity = MagicMock(user_id=7, issuer="https://provider.example", subject="u3")
        claims = {"iss": "https://provider.example", "sub": "u3", "email": "new@example.com", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u3", "email": "new@example.com"})
        db = _FakeSession([existing_identity, existing_user, clashing_user])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertEqual(existing_user.email, "old@example.com")  # kept, didn't collide-overwrite

    async def test_verified_email_links_to_existing_local_user(self):
        """A pre-existing local account with the same verified email is linked."""
        local_user = MagicMock(id=42, email="jane@example.com")
        claims = {"iss": "https://provider.example", "sub": "u-jane", "email": "jane@example.com", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u-jane", "email": "jane@example.com"})
        # execute: identity (none) → email lookup → local_user.
        db = _FakeSession([None, local_user])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        # One row added: the oidc_identity row, bound to user 42. No new user.
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].user_id, 42)
        self.assertEqual(db.added[0].issuer, "https://provider.example")

    async def test_unverified_email_of_existing_user_is_refused(self):
        """IdP says email_verified=false and the email is taken → 403, no link, no duplicate user."""
        local_user = MagicMock(id=42, email="jane@example.com")
        claims = {"iss": "https://provider.example", "sub": "u-jane", "email": "jane@example.com", "email_verified": False}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u-jane", "email": "jane@example.com", "email_verified": False})
        # execute: identity (none) → email lookup → local_user (refused).
        db = _FakeSession([None, local_user])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            with self.assertRaises(oidc.HTTPException) as ctx:
                await oidc.oidc_exchange(_make_request(),
                    oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(db.added, [])

    async def test_missing_email_verified_does_not_link_in_hardened_mode(self):
        """No email_verified claim at all is not proof of ownership when validation is on."""
        local_user = MagicMock(id=42, email="jane@example.com")
        claims = {"iss": "https://provider.example", "sub": "u-jane", "email": "jane@example.com"}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u-jane", "email": "jane@example.com"})
        db = _FakeSession([None, local_user])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            with self.assertRaises(oidc.HTTPException) as ctx:
                await oidc.oidc_exchange(_make_request(),
                    oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(db.added, [])

    async def test_missing_nonce_rejected_in_hardened_mode(self):
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u", "email": "x@example.com"})
        db = _FakeSession([])

        with self._patch_verify({"sub": "u"}) as verify, \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)):
            with self.assertRaises(oidc.HTTPException) as ctx:
                await oidc.oidc_exchange(_make_request(),
                    oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce=None), db)
        self.assertEqual(ctx.exception.status_code, 400)
        verify.assert_not_called()

    async def test_userinfo_sub_mismatch_rejected(self):
        """OIDC Core §5.3.2: userinfo.sub must equal id_token.sub."""
        claims = {"iss": "https://provider.example", "sub": "u-id-token", "email": "x@example.com", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u-userinfo", "email": "x@example.com"})
        db = _FakeSession([])

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            with self.assertRaises(oidc.HTTPException) as ctx:
                await oidc.oidc_exchange(_make_request(),
                    oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_missing_id_token_in_hardened_mode_rejected(self):
        token = _FakeResponse(200, {"access_token": "t"})  # no id_token
        info = _FakeResponse(200, {"sub": "u", "email": "x@example.com"})
        db = _FakeSession([])

        with patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)):
            with self.assertRaises(oidc.HTTPException) as ctx:
                await oidc.oidc_exchange(_make_request(),
                    oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_id_token_validation_error_rejected(self):
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u", "email": "x@example.com"})
        db = _FakeSession([])

        with self._patch_verify_raises(oidc.IdTokenError("nonce mismatch")), \
             patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)):
            with self.assertRaises(oidc.HTTPException) as ctx:
                await oidc.oidc_exchange(_make_request(),
                    oidc.OidcExchangeRequest(code="c", code_verifier="v", nonce="n"), db)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_pkce_verifier_required_when_pkce_enabled(self):
        db = _FakeSession([])
        with self.assertRaises(oidc.HTTPException) as ctx:
            await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier=None, nonce="n"), db)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_pkce_verifier_forwarded_to_idp(self):
        claims = {"iss": "https://provider.example", "sub": "u1", "email": "a@b.c", "email_verified": True}
        token = _FakeResponse(200, {"access_token": "t", "id_token": "x.y.z"})
        info = _FakeResponse(200, {"sub": "u1", "email": "a@b.c"})
        db = _FakeSession([None, None, None, 0])
        fake = _FakeHttpClient(token, info)

        with self._patch_verify(claims), \
             patch("routers.oidc.httpx.AsyncClient", return_value=fake), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="auth", code_verifier="my-verifier", nonce="n"), db)

        self.assertEqual(fake.last_token_body["code_verifier"], "my-verifier")


# ============================================================================
# /exchange - compat path (no discovery / issuer / JWKS configured)
# ============================================================================


class CompatExchangeTests(_OidcTestBase):
    hardened = False

    async def test_compat_mode_still_logs_in_via_userinfo(self):
        """Pre-hardening installs with no OIDC_DISCOVERY_URL must keep working."""
        token = _FakeResponse(200, {"access_token": "t"})  # no id_token required in compat
        info = _FakeResponse(200, {"sub": "u-compat", "email": "legacy@example.com"})
        # In compat mode we have no issuer, so (issuer, sub) binding is skipped
        # and we go straight to email-link / auto-create:
        # execute: email lookup (none) → username unique (none) → count (0).
        db = _FakeSession([None, None, 0])

        with patch("routers.oidc.httpx.AsyncClient", return_value=_FakeHttpClient(token, info)), \
             patch("routers.oidc.create_access_token", return_value="jwt"):
            resp = await oidc.oidc_exchange(_make_request(),
                oidc.OidcExchangeRequest(code="c", code_verifier="v"), db)

        self.assertEqual(resp, {"access_token": "jwt", "expires_in": 7 * 24 * 3600})
        # No oidc_identity row (no issuer in compat), just the user.
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].email, "legacy@example.com")


# ============================================================================
# /authorize + /config
# ============================================================================


class AuthorizeAndConfigTests(_OidcTestBase):
    hardened = True

    async def test_authorize_returns_pkce_pair_and_nonce(self):
        limiter.enabled = False
        resp = await oidc.oidc_authorize(_make_request())
        self.assertTrue(resp.auth_url.startswith("https://provider.example/authorize?"))
        self.assertIn("code_challenge=", resp.auth_url)
        self.assertIn("code_challenge_method=S256", resp.auth_url)
        self.assertIn("nonce=", resp.auth_url)
        self.assertTrue(resp.code_verifier)
        self.assertTrue(resp.nonce)
        self.assertTrue(resp.state)

    async def test_authorize_omits_pkce_when_disabled(self):
        with patch.object(oidc.app_settings, "oidc_pkce", False):
            resp = await oidc.oidc_authorize(_make_request())
        self.assertNotIn("code_challenge=", resp.auth_url)
        self.assertIsNone(resp.code_verifier)

    async def test_config_reports_hardening_flags(self):
        with patch("routers.oidc.effective_end_session_url", AsyncMock(return_value="https://provider.example/logout")):
            resp = await oidc.oidc_config()
        self.assertTrue(resp.enabled)
        self.assertTrue(resp.pkce_enabled)
        self.assertTrue(resp.id_token_validation)
        self.assertTrue(resp.sso_logout_available)
        self.assertIn("post_logout_redirect_uri=", resp.logout_url)
        self.assertIn("client_id=client-id", resp.logout_url)

    async def test_config_logout_absent_without_end_session(self):
        with patch("routers.oidc.effective_end_session_url", AsyncMock(return_value=None)):
            resp = await oidc.oidc_config()
        self.assertFalse(resp.sso_logout_available)
        self.assertIsNone(resp.logout_url)


# ============================================================================
# /unlink
# ============================================================================


class UnlinkTests(_OidcTestBase):
    hardened = True

    async def test_unlink_removes_identity(self):
        ident = MagicMock(id=1, issuer="https://provider.example")
        other = MagicMock(id=2, issuer="https://other.example")
        db = _FakeSession([[ident, other]])
        user = MagicMock(id=1, has_password=True)
        resp = await oidc.unlink_identity(oidc.OidcUnlinkRequest(issuer="https://provider.example"), db, user)
        self.assertEqual(resp, {"status": "unlinked"})
        self.assertIn(ident, db.deleted)

    async def test_unlink_refused_when_it_would_lock_user_out(self):
        """SSO-only, no password, only identity → refuse."""
        ident = MagicMock(id=1, issuer="https://provider.example")
        db = _FakeSession([[ident]])
        user = MagicMock(id=1, has_password=False)
        with patch.object(oidc.app_settings, "oidc_disable_password_login", True):
            with self.assertRaises(oidc.HTTPException) as ctx:
                await oidc.unlink_identity(oidc.OidcUnlinkRequest(issuer="https://provider.example"), db, user)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(db.deleted, [])

    async def test_unlink_404_when_identity_missing(self):
        db = _FakeSession([[]])
        user = MagicMock(id=1, has_password=True)
        with self.assertRaises(oidc.HTTPException) as ctx:
            await oidc.unlink_identity(oidc.OidcUnlinkRequest(issuer="https://nope.example"), db, user)
        self.assertEqual(ctx.exception.status_code, 404)


# ============================================================================
# core.oidc helpers (PKCE, JWKS cache, id_token validation)
# ============================================================================


class CoreOidcHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_pkce_pair_is_s256_of_verifier(self):
        import base64
        import hashlib
        verifier, challenge = core_oidc.generate_pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, expected)
        self.assertGreaterEqual(len(verifier), 43)  # RFC 7636

    def test_nonce_is_url_safe_and_long(self):
        n = core_oidc.generate_nonce()
        self.assertGreaterEqual(len(n), 32)
        self.assertTrue(all(c.isalnum() or c in "-_" for c in n))

    def test_id_token_validation_available_requires_both_sides(self):
        with patch.multiple(core_oidc.app_settings, oidc_discovery_url=None, oidc_issuer=None, oidc_jwks_url=None):
            self.assertFalse(core_oidc.id_token_validation_available())
        with patch.multiple(core_oidc.app_settings, oidc_discovery_url=None, oidc_issuer="https://x", oidc_jwks_url=None):
            self.assertFalse(core_oidc.id_token_validation_available())
        with patch.multiple(core_oidc.app_settings, oidc_discovery_url=None, oidc_issuer="https://x", oidc_jwks_url="https://x/jwks"):
            self.assertTrue(core_oidc.id_token_validation_available())
        with patch.multiple(core_oidc.app_settings, oidc_discovery_url="https://x/.wk", oidc_issuer=None, oidc_jwks_url=None):
            self.assertTrue(core_oidc.id_token_validation_available())

    async def test_jwks_cache_refreshes_on_unknown_kid(self):
        """If a kid lookup misses, we force one refresh (handles IdP key rotation)."""
        core_oidc._jwks_cache.clear()
        responses = [
            {"keys": [{"kid": "old", "kty": "RSA"}]},
            {"keys": [{"kid": "old", "kty": "RSA"}, {"kid": "new", "kty": "RSA"}]},
        ]
        call_count = {"n": 0}

        async def fake_fetch(url, *, timeout=10.0):
            call_count["n"] += 1
            return responses[min(call_count["n"] - 1, len(responses) - 1)]

        with patch("core.oidc._fetch_json", side_effect=fake_fetch):
            jwk = await core_oidc.get_jwk_for_kid("https://x/jwks", "new")
        self.assertIsNotNone(jwk)
        self.assertEqual(jwk["kid"], "new")
        self.assertEqual(call_count["n"], 2)  # original + forced refresh

    async def test_verify_id_token_rejects_disallowed_alg(self):
        with patch("core.oidc.jwt.get_unverified_header", return_value={"alg": "none", "kid": "k"}):
            with self.assertRaises(core_oidc.IdTokenError):
                await core_oidc.verify_id_token("x.y.z", expected_nonce=None, expected_audience="aud")

        with patch("core.oidc.jwt.get_unverified_header", return_value={"alg": "HS256", "kid": "k"}):
            with self.assertRaises(core_oidc.IdTokenError):
                await core_oidc.verify_id_token("x.y.z", expected_nonce=None, expected_audience="aud")

    async def test_verify_id_token_rejects_azp_mismatch(self):
        with patch("core.oidc.jwt.get_unverified_header", return_value={"alg": "RS256", "kid": "k"}), \
             patch("core.oidc.effective_jwks_url", AsyncMock(return_value="https://x/jwks")), \
             patch("core.oidc.effective_issuer", AsyncMock(return_value="https://x")), \
             patch("core.oidc.get_jwk_for_kid", AsyncMock(return_value={"kid": "k"})), \
             patch("core.oidc.jwt.decode", return_value={"sub": "s", "aud": "aud", "azp": "someone-else"}):
            with self.assertRaises(core_oidc.IdTokenError):
                await core_oidc.verify_id_token("x.y.z", expected_nonce=None, expected_audience="aud")

    async def test_verify_id_token_rejects_nonce_mismatch(self):
        with patch("core.oidc.jwt.get_unverified_header", return_value={"alg": "RS256", "kid": "k"}), \
             patch("core.oidc.effective_jwks_url", AsyncMock(return_value="https://x/jwks")), \
             patch("core.oidc.effective_issuer", AsyncMock(return_value="https://x")), \
             patch("core.oidc.get_jwk_for_kid", AsyncMock(return_value={"kid": "k"})), \
             patch("core.oidc.jwt.decode", return_value={"sub": "s", "aud": "aud", "nonce": "wrong"}):
            with self.assertRaises(core_oidc.IdTokenError):
                await core_oidc.verify_id_token("x.y.z", expected_nonce="expected", expected_audience="aud")

    async def test_verify_id_token_happy_path(self):
        claims = {"sub": "s", "aud": "aud", "nonce": "n", "email": "x@y.z"}
        with patch("core.oidc.jwt.get_unverified_header", return_value={"alg": "RS256", "kid": "k"}), \
             patch("core.oidc.effective_jwks_url", AsyncMock(return_value="https://x/jwks")), \
             patch("core.oidc.effective_issuer", AsyncMock(return_value="https://x")), \
             patch("core.oidc.get_jwk_for_kid", AsyncMock(return_value={"kid": "k"})), \
             patch("core.oidc.jwt.decode", return_value=claims):
            out = await core_oidc.verify_id_token("x.y.z", expected_nonce="n", expected_audience="aud")
        self.assertEqual(out, claims)


if __name__ == "__main__":
    unittest.main()
