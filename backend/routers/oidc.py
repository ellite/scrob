"""OIDC authorization-code login.

Flow: /authorize builds the IdP URL (with state/PKCE/nonce), /exchange swaps
the code for a Scrob JWT. When OIDC_DISCOVERY_URL (or OIDC_ISSUER +
OIDC_JWKS_URL) is set, the id_token is signature-verified and identity is
bound on (issuer, sub) via ``oidc_identities``. Otherwise login falls back
to matching users by the OIDC_IDENTIFIER_FIELD userinfo claim and a warning
is logged on first use.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.config import settings as app_settings
from core.limiter import limiter
from core.oidc import (
    IdTokenError,
    effective_end_session_url,
    effective_issuer,
    generate_nonce,
    generate_pkce_pair,
    id_token_validation_available,
    verify_id_token,
)
from core.security import create_access_token, session_lifetime
from db import get_db
from dependencies import get_current_user
from models.oidc_identity import OidcIdentity
from models.users import User


logger = logging.getLogger(__name__)
router = APIRouter()


class OidcAuthorizeResponse(BaseModel):
    auth_url: str
    state: str
    code_verifier: Optional[str] = None
    nonce: Optional[str] = None


class OidcExchangeRequest(BaseModel):
    code: str
    code_verifier: Optional[str] = None
    nonce: Optional[str] = None
    remember: bool = False


class OidcConfigResponse(BaseModel):
    enabled: bool
    provider_name: str
    disable_password_login: bool
    sso_logout_available: bool
    logout_url: Optional[str] = None
    pkce_enabled: bool
    id_token_validation: bool


class OidcIdentityItem(BaseModel):
    issuer: str
    provider_name: Optional[str] = None
    created_at: datetime
    last_login_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OidcUnlinkRequest(BaseModel):
    issuer: str


async def _build_logout_url() -> Optional[str]:
    end_session = await effective_end_session_url()
    if not end_session:
        return None
    # RP-Initiated Logout 1.0 §3 lets us use (client_id, post_logout_redirect_uri)
    # instead of id_token_hint - Scrob doesn't retain the id_token.
    redirect = app_settings.oidc_post_logout_redirect_url or f"{app_settings.server_url.rstrip('/')}/login"
    params = {"post_logout_redirect_uri": redirect}
    if app_settings.oidc_client_id:
        params["client_id"] = app_settings.oidc_client_id
    sep = "&" if "?" in end_session else "?"
    return f"{end_session}{sep}{urlencode(params)}"


_compat_warned = False


def _warn_compat_once() -> None:
    global _compat_warned
    if _compat_warned:
        return
    _compat_warned = True
    logger.warning(
        "OIDC: running in compat mode (no OIDC_DISCOVERY_URL / OIDC_ISSUER+OIDC_JWKS_URL). "
        "id_token signatures are not verified. Set OIDC_DISCOVERY_URL to enable hardening."
    )


async def _resolve_user(
    db: AsyncSession,
    *,
    issuer: Optional[str],
    subject: Optional[str],
    email: Optional[str],
    email_verified: Optional[bool],
    preferred_username: Optional[str],
    legacy_identifier: Optional[str],
) -> User:
    """Returns the User to log in: (issuer, sub) match, else verified-email link, else auto-create."""
    if issuer and subject:
        identity = (await db.execute(
            select(OidcIdentity).where(OidcIdentity.issuer == issuer, OidcIdentity.subject == subject)
        )).scalar_one_or_none()
        if identity:
            user = (await db.execute(select(User).where(User.id == identity.user_id))).scalar_one()
            # Keep User.email in sync when the IdP has a verified new email -
            # never on an unverified claim, never on a collision.
            if email and email != user.email and email_verified is True:
                clash = (await db.execute(
                    select(User).where(User.email == email, User.id != user.id)
                )).scalar_one_or_none()
                if clash:
                    logger.warning("OIDC: email %s for user_id=%s collides; keeping stored email", email, user.id)
                else:
                    user.email = email
            identity.last_login_at = datetime.utcnow()
            return user

    # Link to a local user by email. When OIDC_IDENTIFIER_FIELD is not "email",
    # also try the value of that claim against User.email — some installs keep
    # a non-email identifier (e.g. "sub") in that column.
    link_candidates: list[tuple[str, str]] = []
    if email:
        link_candidates.append(("email", email))
    if (
        legacy_identifier
        and app_settings.oidc_identifier_field != "email"
        and legacy_identifier not in {v for _, v in link_candidates}
    ):
        link_candidates.append(("identifier", legacy_identifier))

    # With id_token validation on, an IdP that omits email_verified is not
    # trusted to have verified the address: linking a local account by email
    # needs an explicit true. Compat mode keeps the old, laxer behaviour.
    require_verified = id_token_validation_available()
    link_refused = False
    for kind, value in link_candidates:
        user = (await db.execute(select(User).where(User.email == value))).scalar_one_or_none()
        if not user:
            continue
        if kind == "email" and (email_verified is False or (require_verified and email_verified is not True)):
            logger.warning("OIDC: refusing to link email=%s (email not verified)", value)
            link_refused = True
            continue
        if issuer and subject:
            db.add(OidcIdentity(
                user_id=user.id, issuer=issuer, subject=subject,
                email_at_link=email, provider_name=app_settings.oidc_provider_name,
                last_login_at=datetime.utcnow(),
            ))
            logger.info("OIDC: linked user_id=%s to issuer=%s via %s", user.id, issuer, kind)
        return user

    if link_refused:
        # The address belongs to an existing account we may not link to; creating
        # a second user with it would only trip the unique email constraint.
        raise HTTPException(status_code=403, detail="Email address is not verified by the identity provider")

    if not app_settings.oidc_auto_create_users:
        raise HTTPException(status_code=403, detail="No account found for this identity")

    user_email = email or legacy_identifier
    if not user_email:
        raise HTTPException(status_code=400, detail="No email claim in id_token / userinfo")

    base_username = str(
        preferred_username or (user_email.split("@")[0] if "@" in user_email else user_email)
    )[:100]
    username = base_username
    counter = 1
    while (await db.execute(select(User).where(User.username == username))).scalar_one_or_none():
        username = f"{base_username}{counter}"
        counter += 1

    # First user in an empty DB becomes admin (#159).
    is_first_user = (await db.execute(select(func.count()).select_from(User))).scalar_one() == 0

    user = User(
        email=user_email,
        username=username,
        password_hash=None,
        api_key=secrets.token_urlsafe(32),
        is_admin=is_first_user,
    )
    db.add(user)
    await db.flush()

    if issuer and subject:
        db.add(OidcIdentity(
            user_id=user.id, issuer=issuer, subject=subject,
            email_at_link=email, provider_name=app_settings.oidc_provider_name,
            last_login_at=datetime.utcnow(),
        ))

    logger.info("OIDC: created user_id=%s username=%s admin=%s", user.id, username, is_first_user)
    return user


@router.get("/config", response_model=OidcConfigResponse)
async def oidc_config():
    logout_url = await _build_logout_url()
    return OidcConfigResponse(
        enabled=app_settings.oidc_enabled,
        provider_name=app_settings.oidc_provider_name,
        disable_password_login=app_settings.oidc_disable_password_login,
        sso_logout_available=bool(logout_url),
        logout_url=logout_url,
        pkce_enabled=app_settings.oidc_pkce,
        id_token_validation=id_token_validation_available(),
    )


@router.get("/authorize", response_model=OidcAuthorizeResponse)
@limiter.limit("30/minute")
async def oidc_authorize(request: Request):
    """Return the IdP authorization URL plus the per-login state, PKCE
    verifier, and nonce.

    The frontend SSR layer stashes all three in HttpOnly cookies and
    redirects the browser to the IdP — the browser never talks to this
    endpoint directly. /exchange later checks the verifier and nonce.
    """
    if not app_settings.oidc_enabled:
        raise HTTPException(status_code=400, detail="OIDC not enabled")
    if not app_settings.oidc_auth_url or not app_settings.oidc_client_id:
        raise HTTPException(status_code=500, detail="OIDC is not fully configured")

    state = secrets.token_urlsafe(32)
    nonce = generate_nonce()
    params = {
        "client_id": app_settings.oidc_client_id,
        "redirect_uri": app_settings.oidc_redirect_url,
        "response_type": "code",
        "scope": app_settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
    }

    code_verifier: Optional[str] = None
    if app_settings.oidc_pkce:
        code_verifier, code_challenge = generate_pkce_pair()
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    return OidcAuthorizeResponse(
        auth_url=f"{app_settings.oidc_auth_url}?{urlencode(params)}",
        state=state,
        code_verifier=code_verifier,
        nonce=nonce,
    )


@router.post("/exchange")
@limiter.limit("10/minute")
async def oidc_exchange(
    request: Request,
    payload: OidcExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Swap an IdP authorization code for a Scrob JWT.

    Called server-to-server by the frontend's oidc-callback SSR page, which
    forwards the PKCE verifier and nonce it stashed in HttpOnly cookies at
    /authorize time. All id_token / nonce / sub checks happen here.
    """
    if not app_settings.oidc_enabled:
        raise HTTPException(status_code=400, detail="OIDC not enabled")
    if not (
        app_settings.oidc_token_url and app_settings.oidc_userinfo_url
        and app_settings.oidc_client_id and app_settings.oidc_client_secret
    ):
        raise HTTPException(status_code=500, detail="OIDC is not fully configured")
    if app_settings.oidc_pkce and not payload.code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE verifier")

    token_body = {
        "grant_type": "authorization_code",
        "code": payload.code,
        "redirect_uri": app_settings.oidc_redirect_url,
        "client_id": app_settings.oidc_client_id,
        "client_secret": app_settings.oidc_client_secret,
    }
    if payload.code_verifier:
        token_body["code_verifier"] = payload.code_verifier

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                app_settings.oidc_token_url,
                data=token_body,
                headers={"Accept": "application/json"},
            )
            if not token_resp.is_success:
                logger.warning("OIDC token exchange failed: status=%s", token_resp.status_code)
                raise HTTPException(status_code=400, detail="Token exchange failed")

            token_json = token_resp.json()
            # IdP-issued access token (for calling userinfo). Not to be confused
            # with the Scrob JWT we return at the end of this handler.
            oidc_access_token = token_json.get("access_token")
            id_token = token_json.get("id_token")
            if not oidc_access_token:
                raise HTTPException(status_code=400, detail="No access token in response")

            claims: dict = {}
            if id_token_validation_available():
                if not id_token:
                    raise HTTPException(status_code=400, detail="IdP did not return an id_token")
                # verify_id_token only checks the nonce when one is supplied, so a
                # missing one must be refused here or the replay check is skipped.
                if not payload.nonce:
                    raise HTTPException(status_code=400, detail="Missing nonce")
                try:
                    claims = await verify_id_token(
                        id_token,
                        expected_nonce=payload.nonce,
                        expected_audience=app_settings.oidc_client_id,
                    )
                except IdTokenError as exc:
                    logger.warning("OIDC id_token rejected: %s", exc)
                    raise HTTPException(status_code=400, detail="id_token validation failed") from exc
            else:
                _warn_compat_once()

            userinfo_resp = await client.get(
                app_settings.oidc_userinfo_url,
                headers={"Authorization": f"Bearer {oidc_access_token}"},
            )
            if not userinfo_resp.is_success:
                raise HTTPException(status_code=400, detail="Failed to fetch user info")
            userinfo: dict = userinfo_resp.json()
    except HTTPException:
        raise
    except Exception:
        logger.exception("OIDC provider connection failed")
        raise HTTPException(status_code=502, detail="Provider connection failed")

    issuer = claims.get("iss") or (await effective_issuer() if id_token_validation_available() else None)
    subject = claims.get("sub") or userinfo.get("sub")
    email = claims.get("email") or userinfo.get("email")
    email_verified = claims.get("email_verified")
    if email_verified is None:
        email_verified = userinfo.get("email_verified")
    preferred_username = (
        claims.get("preferred_username") or claims.get("name")
        or userinfo.get("preferred_username") or userinfo.get("name")
    )
    legacy_identifier = userinfo.get(app_settings.oidc_identifier_field)

    # OIDC Core §5.3.2: userinfo sub must match id_token sub when both are present.
    if claims.get("sub") and userinfo.get("sub") and claims["sub"] != userinfo["sub"]:
        logger.warning("OIDC: userinfo sub does not match id_token sub; rejecting")
        raise HTTPException(status_code=400, detail="userinfo/id_token mismatch")

    if not id_token_validation_available() and not legacy_identifier and not email:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{app_settings.oidc_identifier_field}' not found in user info",
        )

    try:
        user = await _resolve_user(
            db,
            issuer=issuer, subject=subject, email=email,
            email_verified=bool(email_verified) if email_verified is not None else None,
            preferred_username=preferred_username, legacy_identifier=legacy_identifier,
        )
        await db.commit()
    except IntegrityError:
        # Two near-simultaneous first logins for the same (issuer, sub) race
        # on the unique index; retry picks up the row the winner just wrote.
        await db.rollback()
        try:
            user = await _resolve_user(
                db,
                issuer=issuer, subject=subject, email=email,
                email_verified=bool(email_verified) if email_verified is not None else None,
                preferred_username=preferred_username, legacy_identifier=legacy_identifier,
            )
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.warning("OIDC: could not create or link an account for issuer=%s sub=%s", issuer, subject)
            raise HTTPException(status_code=409, detail="Could not create or link an account for this identity")

    lifetime = session_lifetime(payload.remember)
    return {
        "access_token": create_access_token(
            subject=user.id, expires_delta=lifetime, extra_claims={"rem": True} if payload.remember else None
        ),
        "expires_in": int(lifetime.total_seconds()),
    }


@router.get("/identities", response_model=list[OidcIdentityItem])
async def list_identities(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        select(OidcIdentity)
        .where(OidcIdentity.user_id == current_user.id)
        .order_by(OidcIdentity.created_at)
    )).scalars().all()
    return rows


@router.post("/unlink")
async def unlink_identity(
    body: OidcUnlinkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Removes one OIDC identity binding. Refuses if it would lock the user out."""
    identities = (await db.execute(
        select(OidcIdentity).where(OidcIdentity.user_id == current_user.id)
    )).scalars().all()

    target = next((i for i in identities if i.issuer == body.issuer), None)
    if not target:
        raise HTTPException(status_code=404, detail="Identity not found")

    remaining = [i for i in identities if i.id != target.id]
    sso_only = app_settings.oidc_enabled and app_settings.oidc_disable_password_login
    if not current_user.has_password and sso_only and not remaining:
        raise HTTPException(
            status_code=400,
            detail="Cannot unlink: this is your only SSO identity and password login is disabled",
        )

    await db.delete(target)
    await db.commit()
    logger.info("OIDC: unlinked identity for user_id=%s issuer=%s", current_user.id, body.issuer)
    return {"status": "unlinked"}
