import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.config import settings as app_settings
from core.security import create_access_token, get_password_hash
from db import get_db
from models.users import User

router = APIRouter()


class OidcExchangeRequest(BaseModel):
    code: str


@router.get("/config")
async def oidc_config():
    return {
        "enabled": app_settings.oidc_enabled,
        "provider_name": app_settings.oidc_provider_name,
        "disable_password_login": app_settings.oidc_disable_password_login,
    }


@router.get("/authorize")
async def oidc_authorize():
    """
    Returns the provider's authorization URL and a random state token.
    The frontend SSR layer sets the state cookie on the browser and redirects
    to the provider — the browser never talks to this endpoint directly.
    """
    if not app_settings.oidc_enabled:
        raise HTTPException(status_code=400, detail="OIDC not enabled")

    state = secrets.token_urlsafe(32)
    params = {
        "client_id": app_settings.oidc_client_id,
        "redirect_uri": app_settings.oidc_redirect_url,
        "response_type": "code",
        "scope": app_settings.oidc_scopes,
        "state": state,
    }
    auth_url = f"{app_settings.oidc_auth_url}?{urlencode(params)}"
    return {"auth_url": auth_url, "state": state}


@router.post("/exchange")
async def oidc_exchange(
    payload: OidcExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Exchanges an authorization code (already validated by the frontend) for a JWT.
    Called server-to-server by the frontend's oidc-callback SSR page.
    """
    if not app_settings.oidc_enabled:
        raise HTTPException(status_code=400, detail="OIDC not enabled")

    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                app_settings.oidc_token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": payload.code,
                    "redirect_uri": app_settings.oidc_redirect_url,
                    "client_id": app_settings.oidc_client_id,
                    "client_secret": app_settings.oidc_client_secret,
                },
                headers={"Accept": "application/json"},
            )
            if not token_resp.is_success:
                raise HTTPException(status_code=400, detail="Token exchange failed")

            oidc_access_token = token_resp.json().get("access_token")
            if not oidc_access_token:
                raise HTTPException(status_code=400, detail="No access token in response")

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
        raise HTTPException(status_code=502, detail="Provider connection failed")

    identifier = userinfo.get(app_settings.oidc_identifier_field)
    if not identifier:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{app_settings.oidc_identifier_field}' not found in user info",
        )

    result = await db.execute(select(User).where(User.email == str(identifier)))
    user = result.scalar_one_or_none()

    if not user:
        if not app_settings.oidc_auto_create_users:
            raise HTTPException(status_code=403, detail="No account found for this identity")

        raw_email = userinfo.get("email", str(identifier))
        raw_username = (
            userinfo.get("preferred_username")
            or userinfo.get("name")
            or (raw_email.split("@")[0] if "@" in raw_email else raw_email)
        )
        username = str(raw_username)[:100]

        base = username
        counter = 1
        while True:
            exists = await db.execute(select(User).where(User.username == username))
            if not exists.scalar_one_or_none():
                break
            username = f"{base}{counter}"
            counter += 1

        user = User(
            email=str(identifier),
            username=username,
            password_hash=None,
            api_key=secrets.token_urlsafe(32),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    access_token = create_access_token(subject=user.id)
    return {"access_token": access_token}
