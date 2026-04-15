import secrets
import pyotp
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status, Query
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete, func
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import jwt, JWTError

from db import get_db
from models.users import User, UserSettings, TotpBackupCode
from models.email_activation import EmailActivation
from models.password_reset import PasswordResetToken
from core.security import verify_password, get_password_hash, create_access_token, ALGORITHM
from core.config import settings as app_settings
from core.email import send_activation_email, send_password_reset_email
from core.url_validator import validate_service_url
from core.limiter import limiter
import schemas
from dependencies import get_current_user
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)


def _generate_backup_code() -> str:
    """Generate an 8-character alphanumeric backup code formatted as XXXX-XXXX."""
    chars = secrets.token_hex(4).upper()
    return f"{chars[:4]}-{chars[4:]}"


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)

router = APIRouter()


async def _registration_allowed(db: AsyncSession) -> bool:
    """Returns True if registration is currently open."""
    count_result = await db.execute(select(func.count()).select_from(User))
    count = count_result.scalar_one()

    # Always allow the very first user regardless of settings
    if count == 0:
        return True

    if not app_settings.enable_registrations:
        return False

    # 0 means unlimited; otherwise enforce the cap
    if app_settings.registration_max_allowed_users > 0:
        return count < app_settings.registration_max_allowed_users

    return True


@router.get("/registration-status")
async def registration_status(db: AsyncSession = Depends(get_db)):
    allowed = await _registration_allowed(db)
    return {
        "enabled": allowed,
        "smtp_configured": bool(app_settings.smtp_address),
    }


@router.post("/forgot-password")
@limiter.limit("5/minute")
async def forgot_password(request: Request, body: schemas.ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Always returns 200 to avoid leaking whether an email exists."""
    if not app_settings.smtp_address:
        raise HTTPException(status_code=503, detail="Password reset is not configured.")

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user:
        # Remove any existing token for this user
        await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
        token = secrets.token_urlsafe(32)
        db.add(PasswordResetToken(user_id=user.id, token=token))
        await db.commit()
        try:
            await send_password_reset_email(user.email, token)
        except Exception as exc:
            logger.error("Failed to send password reset email to %s: %s", user.email, exc)

    return {"message": "If that email is registered, a reset link has been sent."}


@router.post("/reset-password/{token}")
@limiter.limit("10/minute")
async def reset_password(
    request: Request,
    token: str,
    body: schemas.ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PasswordResetToken).where(PasswordResetToken.token == token))
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=400, detail="invalid")

    age = datetime.now(timezone.utc) - record.created_at.replace(tzinfo=timezone.utc)
    if age > timedelta(hours=1):
        await db.execute(delete(PasswordResetToken).where(PasswordResetToken.token == token))
        await db.commit()
        raise HTTPException(status_code=400, detail="expired")

    user_result = await db.execute(select(User).where(User.id == record.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="invalid")

    user.password_hash = get_password_hash(body.new_password)
    await db.execute(delete(PasswordResetToken).where(PasswordResetToken.token == token))
    await db.commit()
    return {"message": "Password updated successfully."}


@router.post("/register", response_model=schemas.User)
@limiter.limit("10/minute")
async def register(request: Request, user_in: schemas.UserCreate, db: AsyncSession = Depends(get_db)):
    if not await _registration_allowed(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registrations are disabled.",
        )

    query = select(User).where((User.email == user_in.email) | (User.username == user_in.username))
    result = await db.execute(query)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email or username already exists",
        )

    email_confirmed = not app_settings.require_email_validation
    new_user = User(
        email=user_in.email,
        username=user_in.username,
        password_hash=get_password_hash(user_in.password),
        api_key=_generate_api_key(),
        role=user_in.role,
        email_confirmed=email_confirmed,
    )
    db.add(new_user)
    await db.flush()  # get new_user.id before commit

    if app_settings.require_email_validation:
        token = secrets.token_urlsafe(32)
        activation = EmailActivation(user_id=new_user.id, email=new_user.email, token=token)
        db.add(activation)
        await db.commit()
        await db.refresh(new_user, attribute_names=["profile"])
        try:
            await send_activation_email(new_user.email, token)
        except Exception as exc:
            logger.error("Failed to send activation email to %s: %s", new_user.email, exc)
    else:
        await db.commit()
        await db.refresh(new_user, attribute_names=["profile"])

    return new_user

@router.post("/login", response_model=schemas.Token)
@limiter.limit("10/minute")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    if app_settings.oidc_enabled and app_settings.oidc_disable_password_login:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password login is disabled. Please use SSO.",
        )

    query = select(User).where(User.username == form_data.username)
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    if not user or not user.password_hash or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if app_settings.require_email_validation and not user.email_confirmed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not confirmed. Please check your inbox and click the activation link.",
        )

    if user.totp_enabled:
        temp_token = create_access_token(
            subject=user.id,
            expires_delta=timedelta(minutes=10),
            extra_claims={"type": "2fa_pending"},
        )
        return {"requires_2fa": True, "temp_token": temp_token}

    access_token = create_access_token(subject=user.id)
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/activate/{token}", include_in_schema=False)
async def activate_email(token: str, db: AsyncSession = Depends(get_db)):
    frontend = app_settings.server_url
    result = await db.execute(select(EmailActivation).where(EmailActivation.token == token))
    activation = result.scalar_one_or_none()

    if not activation:
        return RedirectResponse(f"{frontend}/auth/activate/{token}?error=invalid")

    age = datetime.now(timezone.utc) - activation.created_at.replace(tzinfo=timezone.utc)
    if age > timedelta(hours=24):
        await db.delete(activation)
        await db.commit()
        return RedirectResponse(f"{frontend}/auth/activate/{token}?error=expired")

    user_result = await db.execute(select(User).where(User.id == activation.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.email_confirmed = True
    await db.delete(activation)
    await db.commit()

    return RedirectResponse(f"{frontend}/auth/activate/{token}?success=true")


@router.post("/activate/{token}", include_in_schema=False)
async def activate_email_api(token: str, db: AsyncSession = Depends(get_db)):
    """JSON endpoint used by the frontend activation page."""
    result = await db.execute(select(EmailActivation).where(EmailActivation.token == token))
    activation = result.scalar_one_or_none()

    if not activation:
        raise HTTPException(status_code=400, detail="invalid")

    age = datetime.now(timezone.utc) - activation.created_at.replace(tzinfo=timezone.utc)
    if age > timedelta(hours=24):
        await db.delete(activation)
        await db.commit()
        raise HTTPException(status_code=400, detail="expired")

    user_result = await db.execute(select(User).where(User.id == activation.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.email_confirmed = True
    await db.delete(activation)
    await db.commit()

    return {"success": True}


@router.get("/me", response_model=schemas.User)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.delete("/me")
async def delete_user_me(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    await db.execute(delete(User).where(User.id == current_user.id))
    await db.commit()
    return {"status": "account deleted"}

def _settings_response(settings: UserSettings) -> schemas.UserSettings:
    """Build a UserSettings schema response, injecting computed fields."""
    data = schemas.UserSettings.model_validate(settings)
    data.trakt_connected = bool(settings.trakt_access_token)
    return data


@router.get("/settings", response_model=schemas.UserSettings)
async def get_user_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = select(UserSettings).where(UserSettings.user_id == current_user.id)
    result = await db.execute(query)
    settings = result.scalar_one_or_none()

    if not settings:
        # Create default settings if they don't exist
        settings = UserSettings(user_id=current_user.id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

    return _settings_response(settings)

@router.patch("/settings", response_model=schemas.UserSettings)
async def update_user_settings(
    settings_in: schemas.UserSettings,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from core import tmdb
    
    query = select(UserSettings).where(UserSettings.user_id == current_user.id)
    result = await db.execute(query)
    settings = result.scalar_one_or_none()
    
    if not settings:
        settings = UserSettings(user_id=current_user.id)
        db.add(settings)

    # Update fields
    # trakt_connected is a read-only computed field; never write it back
    READ_ONLY_FIELDS = {"trakt_connected"}
    update_data = {k: v for k, v in settings_in.model_dump(exclude_unset=True).items() if k not in READ_ONLY_FIELDS}

    # Validate TMDB key if changed
    if "tmdb_api_key" in update_data and update_data["tmdb_api_key"]:
        success = await tmdb.validate_api_key(update_data["tmdb_api_key"])
        if not success:
            raise HTTPException(status_code=400, detail="Invalid TMDB API Key")

    # Validate service URLs to prevent SSRF (cloud metadata credential theft, etc.)
    url_fields = {
        "jellyfin_url": "Jellyfin URL",
        "emby_url": "Emby URL",
        "plex_url": "Plex URL",
        "radarr_url": "Radarr URL",
        "sonarr_url": "Sonarr URL",
    }
    for field, label in url_fields.items():
        if field in update_data and update_data[field]:
            update_data[field] = await validate_service_url(update_data[field], label)

    for field, value in update_data.items():
        if hasattr(settings, field):
            setattr(settings, field, value)
    
    await db.commit()
    await db.refresh(settings)
    return _settings_response(settings)

@router.post("/change-password")
async def change_password(
    password_in: schemas.PasswordUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.password_hash is None:
        # OIDC-created account with no password — allow setting one directly
        if not password_in.current_password:
            current_user.password_hash = get_password_hash(password_in.new_password)
            await db.commit()
            return {"status": "password updated"}
    if not password_in.current_password or not verify_password(password_in.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect current password",
        )
    current_user.password_hash = get_password_hash(password_in.new_password)
    await db.commit()
    return {"status": "password updated"}

@router.post("/api-key/regenerate", response_model=schemas.User)
async def regenerate_api_key(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    current_user.api_key = _generate_api_key()
    await db.commit()
    await db.refresh(current_user)
    return current_user

@router.post("/test-tmdb")
async def test_tmdb(
    key: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    from core import tmdb
    success = await tmdb.validate_api_key(key)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid TMDB API Key")
    return {"status": "ok"}

@router.post("/test-jellyfin")
async def test_jellyfin(
    url: str = Query(...),
    token: str = Query(...),
    user_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user)
):
    from core import jellyfin
    url = await validate_service_url(url, "Jellyfin URL")
    success = await jellyfin.validate_connection(url, token, user_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Jellyfin or invalid User ID")
    return {"status": "ok"}

@router.post("/test-emby")
async def test_emby(
    url: str = Query(...),
    token: str = Query(...),
    user_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user)
):
    from core import emby
    url = await validate_service_url(url, "Emby URL")
    success = await emby.validate_connection(url, token, user_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Emby or invalid User ID")
    return {"status": "ok"}

@router.post("/test-plex")
async def test_plex(
    url: str = Query(...),
    token: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    from core import plex
    url = await validate_service_url(url, "Plex URL")
    success = await plex.validate_connection(url, token)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Plex")
    return {"status": "ok"}

@router.post("/test-radarr")
async def test_radarr(
    url: str = Query(...),
    token: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    from core import radarr
    url = await validate_service_url(url, "Radarr URL")
    success = await radarr.validate_connection(url, token)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Radarr")
    return {"status": "ok"}

@router.get("/radarr/profiles")
async def get_radarr_profiles(
    url: str = Query(...),
    token: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    from core import radarr
    url = await validate_service_url(url, "Radarr URL")
    quality_profiles = await radarr.get_quality_profiles(url, token)
    root_folders = await radarr.get_root_folders(url, token)
    tags = await radarr.get_tags(url, token)
    return {
        "quality_profiles": quality_profiles,
        "root_folders": root_folders,
        "tags": tags
    }

@router.post("/test-sonarr")
async def test_sonarr(
    url: str = Query(...),
    token: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    from core import sonarr
    url = await validate_service_url(url, "Sonarr URL")
    success = await sonarr.validate_connection(url, token)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Sonarr")
    return {"status": "ok"}

@router.get("/connection-status")
async def get_connection_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    import asyncio
    from core import jellyfin as jf, emby as emby_client, plex as px, radarr as rdr, sonarr as snr

    query = select(UserSettings).where(UserSettings.user_id == current_user.id)
    result = await db.execute(query)
    user_settings = result.scalar_one_or_none()

    if not user_settings:
        return {
            "jellyfin": {"configured": False, "connected": False},
            "emby": {"configured": False, "connected": False},
            "plex": {"configured": False, "connected": False},
            "radarr": {"configured": False, "connected": False},
            "sonarr": {"configured": False, "connected": False},
            "trakt": {"configured": False, "connected": False},
        }

    async def check_jellyfin():
        if not (user_settings.jellyfin_url and user_settings.jellyfin_token):
            return {"configured": False, "connected": False}
        connected = await jf.validate_connection(user_settings.jellyfin_url, user_settings.jellyfin_token, user_settings.jellyfin_user_id)
        return {"configured": True, "connected": connected}

    async def check_emby():
        if not (user_settings.emby_url and user_settings.emby_token):
            return {"configured": False, "connected": False}
        connected = await emby_client.validate_connection(user_settings.emby_url, user_settings.emby_token, user_settings.emby_user_id)
        return {"configured": True, "connected": connected}

    async def check_plex():
        if not (user_settings.plex_url and user_settings.plex_token):
            return {"configured": False, "connected": False}
        connected = await px.validate_connection(user_settings.plex_url, user_settings.plex_token)
        return {"configured": True, "connected": connected}

    async def check_radarr():
        if not (user_settings.radarr_url and user_settings.radarr_token):
            return {"configured": False, "connected": False}
        connected = await rdr.validate_connection(user_settings.radarr_url, user_settings.radarr_token)
        if not connected:
            return {"configured": True, "connected": False}
        quality_profiles, root_folders, tags = await asyncio.gather(
            rdr.get_quality_profiles(user_settings.radarr_url, user_settings.radarr_token),
            rdr.get_root_folders(user_settings.radarr_url, user_settings.radarr_token),
            rdr.get_tags(user_settings.radarr_url, user_settings.radarr_token),
        )
        return {"configured": True, "connected": True, "quality_profiles": quality_profiles, "root_folders": root_folders, "tags": tags}

    async def check_sonarr():
        if not (user_settings.sonarr_url and user_settings.sonarr_token):
            return {"configured": False, "connected": False}
        connected = await snr.validate_connection(user_settings.sonarr_url, user_settings.sonarr_token)
        if not connected:
            return {"configured": True, "connected": False}
        quality_profiles, root_folders, tags = await asyncio.gather(
            snr.get_quality_profiles(user_settings.sonarr_url, user_settings.sonarr_token),
            snr.get_root_folders(user_settings.sonarr_url, user_settings.sonarr_token),
            snr.get_tags(user_settings.sonarr_url, user_settings.sonarr_token),
        )
        return {"configured": True, "connected": True, "quality_profiles": quality_profiles, "root_folders": root_folders, "tags": tags}

    async def check_trakt():
        from core import trakt as trakt_client
        if not (user_settings.trakt_access_token and user_settings.trakt_client_id):
            return {"configured": False, "connected": False}
        connected = await trakt_client.validate_token(user_settings.trakt_client_id, user_settings.trakt_access_token)
        return {"configured": True, "connected": connected}

    jf_status, emby_status, px_status, rdr_status, snr_status, trakt_status = await asyncio.gather(
        check_jellyfin(), check_emby(), check_plex(), check_radarr(), check_sonarr(), check_trakt()
    )

    return {"jellyfin": jf_status, "emby": emby_status, "plex": px_status, "radarr": rdr_status, "sonarr": snr_status, "trakt": trakt_status}


@router.get("/sonarr/profiles")
async def get_sonarr_profiles(
    url: str = Query(...),
    token: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    from core import sonarr
    url = await validate_service_url(url, "Sonarr URL")
    quality_profiles = await sonarr.get_quality_profiles(url, token)
    root_folders = await sonarr.get_root_folders(url, token)
    tags = await sonarr.get_tags(url, token)
    return {
        "quality_profiles": quality_profiles,
        "root_folders": root_folders,
        "tags": tags
    }


# --- 2FA endpoints ---

@router.post("/2fa/setup", response_model=schemas.TotpSetupResponse)
async def totp_setup(current_user: User = Depends(get_current_user)):
    """Generate a fresh TOTP secret and provisioning URI. Does not persist anything."""
    if current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=current_user.email,
        issuer_name="Scrob",
    )
    return {"provisioning_uri": uri, "secret": secret}


@router.post("/2fa/enable", response_model=schemas.TotpBackupCodesResponse)
@limiter.limit("10/minute")
async def totp_enable(
    request: Request,
    req: schemas.TotpEnableRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")
    if not pyotp.TOTP(req.secret).verify(req.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid verification code")

    current_user.totp_secret = req.secret
    current_user.totp_enabled = True

    await db.execute(delete(TotpBackupCode).where(TotpBackupCode.user_id == current_user.id))

    new_codes: list[TotpBackupCode] = []
    for _ in range(10):
        bc = TotpBackupCode(user_id=current_user.id, code=_generate_backup_code())
        db.add(bc)
        new_codes.append(bc)

    await db.commit()
    for bc in new_codes:
        await db.refresh(bc)

    return {"codes": new_codes}


@router.post("/2fa/disable")
async def totp_disable(
    req: schemas.TotpDisableRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is not enabled")

    valid = pyotp.TOTP(current_user.totp_secret).verify(req.code, valid_window=1)

    if not valid:
        # Try backup code
        result = await db.execute(
            select(TotpBackupCode).where(
                TotpBackupCode.user_id == current_user.id,
                TotpBackupCode.code == req.code,
                TotpBackupCode.used.is_(False),
            )
        )
        valid = result.scalar_one_or_none() is not None

    if not valid:
        raise HTTPException(status_code=400, detail="Invalid code")

    current_user.totp_enabled = False
    current_user.totp_secret = None
    await db.execute(delete(TotpBackupCode).where(TotpBackupCode.user_id == current_user.id))
    await db.commit()
    return {"status": "2FA disabled"}


@router.get("/2fa/backup-codes", response_model=schemas.TotpBackupCodesResponse)
async def get_backup_codes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is not enabled")
    result = await db.execute(
        select(TotpBackupCode)
        .where(TotpBackupCode.user_id == current_user.id)
        .order_by(TotpBackupCode.id)
    )
    return {"codes": result.scalars().all()}


@router.post("/2fa/verify-login", response_model=schemas.Token)
@limiter.limit("10/minute")
async def verify_2fa_login(
    request: Request,
    req: schemas.TotpVerifyLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
    )
    try:
        payload = jwt.decode(req.temp_token, app_settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("type") != "2fa_pending":
            raise credentials_exception
        user_id = int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.totp_enabled:
        raise credentials_exception

    # Try TOTP code
    if pyotp.TOTP(user.totp_secret).verify(req.code, valid_window=1):
        return {"access_token": create_access_token(subject=user.id), "token_type": "bearer"}

    # Try backup code
    bc_result = await db.execute(
        select(TotpBackupCode).where(
            TotpBackupCode.user_id == user.id,
            TotpBackupCode.code == req.code,
            TotpBackupCode.used.is_(False),
        )
    )
    bc = bc_result.scalar_one_or_none()
    if bc:
        bc.used = True
        await db.commit()
        return {"access_token": create_access_token(subject=user.id), "token_type": "bearer"}

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid verification code")
