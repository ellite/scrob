"""OIDC helpers: PKCE, discovery, JWKS cache, id_token verification."""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import Any, Optional

import httpx
from jose import jwt, JWTError

from core.config import settings as app_settings


logger = logging.getLogger(__name__)

# Asymmetric only. ``none`` and HMAC families are never accepted - verifying
# an HS-signed id_token uses the client secret, which gives us no guarantee
# the IdP (as opposed to anyone else with the secret) produced the token.
ALLOWED_ID_TOKEN_ALGS = ("RS256", "RS384", "RS512", "ES256", "ES384")
CLOCK_SKEW_LEEWAY_SECONDS = 120
_CACHE_TTL_SECONDS = 10 * 60

_discovery_cache: dict[str, tuple[dict, float]] = {}
_jwks_cache: dict[str, tuple[dict, float]] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


def _redact(value: Optional[str]) -> str:
    if not value:
        return "<none>"
    return f"{value[:4]}…({len(value)} chars)" if len(value) > 8 else "<redacted>"


async def _fetch_json(url: str, *, timeout: float = 10.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


async def _cached_fetch(cache: dict[str, tuple[dict, float]], url: str, *, force: bool = False) -> Optional[dict]:
    now = time.monotonic()
    cached = cache.get(url)
    if not force and cached and (now - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]
    try:
        doc = await _fetch_json(url)
    except Exception as exc:
        logger.warning("OIDC fetch failed for %s: %s", url, exc)
        return cached[0] if cached else None
    cache[url] = (doc, now)
    return doc


async def get_discovery_document() -> Optional[dict]:
    if not app_settings.oidc_discovery_url:
        return None
    return await _cached_fetch(_discovery_cache, app_settings.oidc_discovery_url)


async def effective_issuer() -> Optional[str]:
    if app_settings.oidc_issuer:
        return app_settings.oidc_issuer
    doc = await get_discovery_document()
    return doc.get("issuer") if doc else None


async def effective_jwks_url() -> Optional[str]:
    if app_settings.oidc_jwks_url:
        return app_settings.oidc_jwks_url
    doc = await get_discovery_document()
    return doc.get("jwks_uri") if doc else None


async def effective_end_session_url() -> Optional[str]:
    if app_settings.oidc_logout_url:
        return app_settings.oidc_logout_url
    doc = await get_discovery_document()
    return doc.get("end_session_endpoint") if doc else None


async def get_jwk_for_kid(jwks_url: str, kid: Optional[str]) -> Optional[dict]:
    """Looks up a JWK by kid, forcing one JWKS refresh on miss (IdP key rotation)."""
    def _find(doc: Optional[dict]) -> Optional[dict]:
        if not doc:
            return None
        keys = doc.get("keys", [])
        # Spec-compliant IdPs sometimes publish a single key without a kid.
        if kid is None and len(keys) == 1:
            return keys[0]
        return next((k for k in keys if k.get("kid") == kid), None)

    found = _find(await _cached_fetch(_jwks_cache, jwks_url))
    if found is not None:
        return found
    return _find(await _cached_fetch(_jwks_cache, jwks_url, force=True))


class IdTokenError(Exception):
    pass


async def verify_id_token(
    id_token: str,
    *,
    expected_nonce: Optional[str],
    expected_audience: str,
) -> dict[str, Any]:
    """Validates signature + iss/aud/exp/nonce; returns the parsed claims."""
    try:
        header = jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise IdTokenError(f"malformed id_token header: {exc}") from exc

    alg = header.get("alg")
    if alg not in ALLOWED_ID_TOKEN_ALGS:
        raise IdTokenError(f"disallowed alg {alg!r}")

    jwks_url = await effective_jwks_url()
    issuer = await effective_issuer()
    if not jwks_url or not issuer:
        raise IdTokenError("JWKS URL or issuer not configured")

    jwk = await get_jwk_for_kid(jwks_url, header.get("kid"))
    if not jwk:
        raise IdTokenError(f"no JWKS key for kid={header.get('kid')!r}")

    try:
        claims = jwt.decode(
            id_token,
            jwk,
            algorithms=list(ALLOWED_ID_TOKEN_ALGS),
            audience=expected_audience,
            issuer=issuer,
            options={"verify_at_hash": False, "leeway": CLOCK_SKEW_LEEWAY_SECONDS},
        )
    except JWTError as exc:
        raise IdTokenError(f"signature/claims rejected: {exc}") from exc

    # OIDC Core §3.1.3.7: when ``azp`` is present it must equal our client_id,
    # even if ``aud`` already contains only our client_id.
    azp = claims.get("azp")
    if azp is not None and azp != expected_audience:
        raise IdTokenError(f"azp {azp!r} does not match client_id")

    if expected_nonce is not None and claims.get("nonce") != expected_nonce:
        raise IdTokenError("nonce mismatch")

    return claims


def id_token_validation_available() -> bool:
    """True iff config has enough to validate id_tokens (issuer + JWKS)."""
    has_issuer = bool(app_settings.oidc_issuer or app_settings.oidc_discovery_url)
    has_jwks = bool(app_settings.oidc_jwks_url or app_settings.oidc_discovery_url)
    return has_issuer and has_jwks
