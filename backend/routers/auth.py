import secrets
import pyotp
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete, func, or_, and_
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import jwt, JWTError

from db import get_db
from models.base import UserRole
from models.users import User, UserSettings, TotpBackupCode
from models.global_settings import GlobalSettings
from models.connections import MediaServerConnection
from models.scrobble_connection import ScrobbleConnection
from models.email_activation import EmailActivation
from models.password_reset import PasswordResetToken
from models.oauth_device import OAuthDeviceGrant
from core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    hash_opaque_token,
    generate_opaque_token,
    ALGORITHM,
)
from core.config import settings as app_settings
from core.email import send_activation_email, send_password_reset_email
from core.url_validator import validate_service_url
from core.limiter import limiter
from core.backup import restore_backup
from core.nuvio import NuvioAPIError, parse_profile_id
import schemas
from dependencies import get_current_user, DEVICE_TOKEN_TYPE
from sqlalchemy.orm import selectinload
from fastapi import File, UploadFile

logger = logging.getLogger(__name__)


def _generate_backup_code() -> str:
    """Generate an 8-character alphanumeric backup code formatted as XXXX-XXXX."""
    chars = secrets.token_hex(4).upper()
    return f"{chars[:4]}-{chars[4:]}"


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)

router = APIRouter()


def _prevent_sensitive_response_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _parse_nuvio_profile_id(value: str | None) -> int:
    try:
        return parse_profile_id(value)
    except NuvioAPIError:
        raise HTTPException(status_code=400, detail="Nuvio profile must be an integer from 1 to 6")


def _nuvio_profile_name(profiles: list[dict], profile_id: int) -> str:
    for profile in profiles:
        try:
            matches = int(profile.get("profile_index") or 0) == profile_id
        except (TypeError, ValueError):
            continue
        if matches:
            name = str(profile.get("name") or "").strip()
            return name or f"Profile {profile_id}"
    return f"Profile {profile_id}"




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

    count_result = await db.execute(select(func.count()).select_from(User))
    is_first_user = count_result.scalar_one() == 0

    email_confirmed = not app_settings.require_email_validation
    new_user = User(
        email=user_in.email,
        username=user_in.username,
        password_hash=get_password_hash(user_in.password),
        api_key=_generate_api_key(),
        # Never take the role from the request body: role == "admin" grants
        # elevated access in several routers independently of is_admin, so a
        # self-registering user could escalate by posting {"role": "admin"}.
        # The first user is the bootstrap admin, so it gets both is_admin and
        # the admin role (some routers check role, others is_admin).
        role=UserRole.admin if is_first_user else UserRole.user,
        is_admin=is_first_user,
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


@router.get("/has-users")
async def has_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(func.count()).select_from(User))
    return {"has_users": result.scalar_one() > 0}


@router.post("/bootstrap-restore")
async def bootstrap_restore(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    count_result = await db.execute(select(func.count()).select_from(User))
    if count_result.scalar_one() > 0:
        raise HTTPException(status_code=403, detail="Bootstrap restore is only available when no users exist.")

    if not (file.filename or "").endswith(".bak"):
        raise HTTPException(status_code=400, detail="Only .bak backup files are accepted.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    await db.rollback()

    try:
        await restore_backup(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "restored"}


@router.get("/me", response_model=schemas.User)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.delete("/me")
async def delete_user_me(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.is_admin:
        total_result = await db.execute(select(func.count()).select_from(User))
        if total_result.scalar_one() > 1:
            admin_result = await db.execute(
                select(func.count()).select_from(User).where(User.is_admin.is_(True))
            )
            if admin_result.scalar_one() <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="You are the sole admin. Promote another user to admin before deleting your account.",
                )
    await db.execute(delete(User).where(User.id == current_user.id))
    await db.commit()
    return {"status": "account deleted"}

async def _settings_response(settings: UserSettings, db: AsyncSession) -> schemas.UserSettings:
    """Build a UserSettings schema response, injecting computed fields."""
    data = schemas.UserSettings.model_validate(settings)
    data.trakt_connected = bool(settings.trakt_access_token)
    data.simkl_connected = bool(settings.simkl_access_token)
    data.mdblist_connected = bool(settings.mdblist_api_key)
    data.bingebase_connected = bool(settings.bingebase_webhook_url or settings.bingebase_api_key)
    gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = gs_result.scalar_one_or_none()
    data.has_rpdb_key = bool(settings.rpdb_api_key)
    data.has_global_tmdb_key = bool(gs and gs.tmdb_api_key)
    data.has_effective_tmdb_key = bool(settings.tmdb_api_key) or data.has_global_tmdb_key
    data.has_global_tvdb_key = bool(gs and gs.tvdb_api_key)
    data.has_effective_tvdb_key = bool(settings.tvdb_api_key) or data.has_global_tvdb_key
    # Same "all 4 fields set, user config first" rule as _effective_radarr/
    # _effective_sonarr in routers/media.py - inlined rather than imported to
    # avoid a routers.media <-> routers.auth cross-import.
    data.has_effective_radarr = bool(
        all([settings.radarr_url, settings.radarr_token, settings.radarr_root_folder, settings.radarr_quality_profile])
        or (gs and all([gs.radarr_url, gs.radarr_token, gs.radarr_root_folder, gs.radarr_quality_profile]))
    )
    data.has_effective_sonarr = bool(
        all([settings.sonarr_url, settings.sonarr_token, settings.sonarr_root_folder, settings.sonarr_quality_profile])
        or (gs and all([gs.sonarr_url, gs.sonarr_token, gs.sonarr_root_folder, gs.sonarr_quality_profile]))
    )
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

    return await _settings_response(settings, db)

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

    # Computed read-only fields; never write them back
    READ_ONLY_FIELDS = {"trakt_connected", "simkl_connected", "mdblist_connected", "bingebase_connected", "has_rpdb_key", "has_global_tmdb_key", "has_effective_tmdb_key", "has_global_tvdb_key", "has_effective_tvdb_key"}
    update_data = {k: v for k, v in settings_in.model_dump(exclude_unset=True).items() if k not in READ_ONLY_FIELDS}

    new_rpdb_key = update_data.get("rpdb_api_key")
    if new_rpdb_key and new_rpdb_key != settings.rpdb_api_key:
        from core import rpdb
        if not await rpdb.validate_api_key(new_rpdb_key):
            raise HTTPException(status_code=400, detail="RPDB could not validate this API key. Check the key and try again.")

    if "tmdb_api_key" in update_data and update_data["tmdb_api_key"]:
        success = await tmdb.validate_api_key(update_data["tmdb_api_key"])
        if not success:
            raise HTTPException(status_code=400, detail="Invalid TMDB API Key")

    # Validate the resulting TVDB key/PIN pair whenever either is touched - but
    # not when the key is being cleared (nothing to validate then).
    tvdb_touched = "tvdb_api_key" in update_data or "tvdb_subscriber_pin" in update_data
    new_tvdb_key = update_data["tvdb_api_key"] if "tvdb_api_key" in update_data else settings.tvdb_api_key
    new_tvdb_pin = update_data["tvdb_subscriber_pin"] if "tvdb_subscriber_pin" in update_data else settings.tvdb_subscriber_pin
    if tvdb_touched and new_tvdb_key:
        from core import tvdb
        if not await tvdb.validate_api_key(new_tvdb_key, pin=new_tvdb_pin or None):
            raise HTTPException(
                status_code=400,
                detail="TVDB rejected the key" + (" / PIN" if new_tvdb_pin else "")
                + ". A subscriber-supported key needs its account PIN; a free project key needs no PIN.",
            )

    if "mdblist_api_key" in update_data and update_data["mdblist_api_key"]:
        from core import mdblist
        if not await mdblist.validate_api_key(update_data["mdblist_api_key"]):
            raise HTTPException(status_code=400, detail="Invalid MDBList API key")

    url_fields = {"radarr_url": "Radarr URL", "sonarr_url": "Sonarr URL", "bingebase_webhook_url": "Bingebase Webhook URL"}
    for field, label in url_fields.items():
        if field in update_data and update_data[field]:
            update_data[field] = await validate_service_url(update_data[field], label)

    for field, value in update_data.items():
        if hasattr(settings, field):
            setattr(settings, field, value)

    await db.commit()
    await db.refresh(settings)
    return await _settings_response(settings, db)


# ── Media Server Connection CRUD ───────────────────────────────────────────────

@router.get("/connections", response_model=list[schemas.MediaServerConnectionResponse])
async def list_connections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(MediaServerConnection)
        .where(MediaServerConnection.user_id == current_user.id)
        .order_by(MediaServerConnection.created_at)
    )
    return result.scalars().all()


@router.post("/connections", response_model=schemas.MediaServerConnectionResponse, status_code=201)
async def create_connection(
    body: schemas.MediaServerConnectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.type not in ("plex", "jellyfin", "emby", "nuvio", "stremio", "arvio"):
        raise HTTPException(
            status_code=400,
            detail="type must be plex, jellyfin, emby, nuvio, stremio, or arvio",
        )
    validated_url = body.url
    connection_token = body.token
    server_user_id = body.server_user_id
    server_username = body.server_username
    if body.type == "stremio":
        from core import stremio

        existing_result = await db.execute(
            select(MediaServerConnection).where(
                MediaServerConnection.user_id == current_user.id,
                MediaServerConnection.type == "stremio",
            )
        )
        if existing_result.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Stremio is already connected")
        try:
            account = await stremio.validate_auth_key(connection_token)
        except stremio.StremioAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        validated_url = stremio.DEFAULT_URL
        server_user_id = str(account["_id"])
        server_username = str(account.get("email") or "Stremio")
    else:
        validated_url = await validate_service_url(
            body.url,
            f"{body.type.capitalize()} URL",
        )
    if body.type == "nuvio":
        from core import nuvio

        profile_id = _parse_nuvio_profile_id(server_user_id)
        try:
            session, profiles = await nuvio.validate_connection(validated_url, connection_token, profile_id)
        except nuvio.NuvioAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        connection_token = session.refresh_token
        server_user_id = str(profile_id)
        server_username = _nuvio_profile_name(profiles, profile_id)
    elif body.type == "arvio":
        from core import arvio

        profile_id = str(server_user_id or "")
        if not profile_id:
            raise HTTPException(status_code=400, detail="ARVIO profile ID is required")
        try:
            session, profiles = await arvio.validate_connection(validated_url, connection_token, profile_id)
        except arvio.ArvioAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        connection_token = session.refresh_token
        server_user_id = profile_id
        server_username = arvio.get_profile_name(profiles, profile_id)

    plex_auth_token = None
    plex_account_id = None
    plex_machine_identifier = None
    if body.type == "plex":
        plex_auth_token = body.plex_auth_token or None
        plex_account_id = body.plex_account_id or None
        plex_machine_identifier = body.plex_machine_identifier or None
        if plex_machine_identifier:
            dup = await db.execute(
                select(MediaServerConnection).where(
                    MediaServerConnection.user_id == current_user.id,
                    MediaServerConnection.type == "plex",
                    MediaServerConnection.plex_machine_identifier == plex_machine_identifier,
                )
            )
            if dup.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="This Plex server is already connected.")

    cloud_media_provider = body.type in ("nuvio", "stremio", "arvio")
    conn = MediaServerConnection(
        user_id=current_user.id,
        type=body.type,
        name=body.name,
        url=validated_url,
        token=connection_token,
        server_user_id=server_user_id,
        server_username=server_username,
        plex_auth_token=plex_auth_token,
        plex_account_id=plex_account_id,
        plex_machine_identifier=plex_machine_identifier,
        sync_collection=body.sync_collection,
        sync_watched=body.sync_watched,
        sync_ratings=body.sync_ratings if not cloud_media_provider else False,
        sync_playback=body.sync_playback,
        push_watched=body.push_watched,
        push_collection=body.push_collection if cloud_media_provider else False,
        push_playback=body.push_playback if cloud_media_provider else False,
        push_ratings=body.push_ratings if not cloud_media_provider else False,
        auto_sync_interval=body.auto_sync_interval,
        auto_push_interval=body.auto_push_interval,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


@router.patch("/connections/{connection_id}", response_model=schemas.MediaServerConnectionResponse)
async def update_connection(
    connection_id: int,
    body: schemas.MediaServerConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.id == connection_id,
            MediaServerConnection.user_id == current_user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    update_data = body.model_dump(exclude_unset=True)
    if conn.type == "stremio":
        from core import stremio

        update_data["url"] = stremio.DEFAULT_URL
        candidate_token = update_data.get("token")
        if candidate_token:
            try:
                account = await stremio.validate_auth_key(candidate_token)
            except stremio.StremioAPIError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            update_data["server_user_id"] = str(account["_id"])
            update_data["server_username"] = str(account.get("email") or "Stremio")
        else:
            update_data.pop("token", None)
        update_data["sync_ratings"] = False
        update_data["push_ratings"] = False
        update_data["push_playback"] = update_data.get("push_playback", conn.push_playback)
        update_data["push_collection"] = update_data.get("push_collection", conn.push_collection)
    else:
        if "url" in update_data and update_data["url"]:
            update_data["url"] = await validate_service_url(
                update_data["url"],
                f"{conn.type.capitalize()} URL",
            )

        if conn.type == "nuvio":
            from core import nuvio

            candidate_url = update_data.get("url", conn.url)
            profile_id = _parse_nuvio_profile_id(update_data.get("server_user_id", conn.server_user_id))

            async def _persist_refresh(session: nuvio.NuvioSession) -> None:
                # Persist the rotated token the moment it exists — if the
                # profile lookup that follows fails, the connection must not
                # be left holding a refresh token Nuvio has already redeemed.
                conn.token = session.refresh_token
                await db.commit()

            try:
                async with nuvio.connection_lock(conn.id):
                    # Refresh_token is single-use and rotates on every redeem
                    # (see core/nuvio.py's connection_lock docstring) - conn
                    # may have been loaded before another request (e.g. the
                    # connections page's status check) already rotated it
                    # while this one waited for the lock. Re-read the latest
                    # persisted value now, inside the lock, rather than reuse
                    # whatever was loaded at request start.
                    await db.refresh(conn)
                    candidate_token = update_data.get("token", conn.token)
                    session, profiles = await nuvio.validate_connection(
                        candidate_url, candidate_token, profile_id, on_refresh=_persist_refresh
                    )
            except nuvio.NuvioAPIError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            update_data["token"] = session.refresh_token
            update_data["server_user_id"] = str(profile_id)
            update_data["server_username"] = _nuvio_profile_name(profiles, profile_id)
            update_data["sync_ratings"] = False
            update_data["push_ratings"] = False
            update_data["push_playback"] = update_data.get("push_playback", conn.push_playback)
            update_data["push_collection"] = update_data.get("push_collection", conn.push_collection)
        else:
            update_data["push_playback"] = False
            update_data["push_collection"] = False

    # Re-enabling a watchlist sync direction starts from a clean bootstrap: a
    # baseline recorded under the old settings must not drive deletions.
    for flag in ("plex_sync_watchlist", "plex_push_watchlist"):
        if update_data.get(flag) and not getattr(conn, flag):
            update_data["plex_watchlist_synced_keys"] = None
            break

    for field, value in update_data.items():
        setattr(conn, field, value)

    await db.commit()
    await db.refresh(conn)
    return conn


@router.delete("/connections/{connection_id}")
async def delete_connection(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.id == connection_id,
            MediaServerConnection.user_id == current_user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if conn.type == "stremio":
        from core import stremio

        try:
            await stremio.logout(conn.token)
        except stremio.StremioAPIError:
            logger.warning(
                "Failed to revoke Stremio session for connection %s",
                conn.id,
            )
    await db.delete(conn)
    await db.commit()
    return {"status": "deleted"}


# ── Scrobble-only connections ──────────────────────────────────────────────────

@router.get("/scrobble-connections", response_model=list[schemas.ScrobbleConnectionResponse])
async def list_scrobble_connections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ScrobbleConnection)
        .where(ScrobbleConnection.user_id == current_user.id)
        .order_by(ScrobbleConnection.created_at)
    )
    return result.scalars().all()


@router.post("/scrobble-connections", response_model=schemas.ScrobbleConnectionResponse, status_code=201)
async def create_scrobble_connection(
    body: schemas.ScrobbleConnectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.type not in ("plex", "jellyfin", "emby"):
        raise HTTPException(status_code=400, detail="type must be plex, jellyfin, or emby")
    conn = ScrobbleConnection(
        user_id=current_user.id,
        type=body.type,
        name=body.name,
        server_user_id=body.server_user_id,
        server_username=body.server_username,
        sync_collection=body.sync_collection,
        sync_watched=body.sync_watched,
        sync_playback=body.sync_playback,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


@router.patch("/scrobble-connections/{connection_id}", response_model=schemas.ScrobbleConnectionResponse)
async def update_scrobble_connection(
    connection_id: int,
    body: schemas.ScrobbleConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ScrobbleConnection).where(
            ScrobbleConnection.id == connection_id,
            ScrobbleConnection.user_id == current_user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Scrobble connection not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(conn, field, value)
    await db.commit()
    await db.refresh(conn)
    return conn


@router.delete("/scrobble-connections/{connection_id}")
async def delete_scrobble_connection(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ScrobbleConnection).where(
            ScrobbleConnection.id == connection_id,
            ScrobbleConnection.user_id == current_user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Scrobble connection not found")
    await db.delete(conn)
    await db.commit()
    return {"status": "deleted"}


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
    body: schemas.ApiKeyTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import tmdb
    _prevent_sensitive_response_caching(response)
    success = await tmdb.validate_api_key(body.key.get_secret_value())
    if not success:
        raise HTTPException(status_code=400, detail="Invalid TMDB API Key")
    return {"status": "ok", "message": "TMDB API key is valid."}

@router.post("/test-rpdb")
async def test_rpdb(
    body: schemas.ApiKeyTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import rpdb
    _prevent_sensitive_response_caching(response)
    return {"success": await rpdb.validate_api_key(body.key.get_secret_value())}

@router.post("/test-tvdb")
async def test_tvdb(
    body: schemas.ApiKeyTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import tvdb
    _prevent_sensitive_response_caching(response)
    pin = body.pin.get_secret_value() if body.pin else None
    success = await tvdb.validate_api_key(body.key.get_secret_value(), pin=pin)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="TVDB rejected the key" + (" / PIN" if pin else "")
            + ". A subscriber-supported key needs its account PIN; a free project key needs no PIN.",
        )
    return {"status": "ok", "message": "TVDB API key is valid."}

@router.post("/test-jellyfin")
async def test_jellyfin(
    body: schemas.ServiceConnectionTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import jellyfin
    _prevent_sensitive_response_caching(response)
    url = await validate_service_url(body.url, "Jellyfin URL")
    success = await jellyfin.validate_connection(
        url,
        body.token.get_secret_value(),
        body.user_id,
    )
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Jellyfin or invalid User ID")
    return {"status": "ok"}

@router.post("/test-emby")
async def test_emby(
    body: schemas.ServiceConnectionTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import emby
    _prevent_sensitive_response_caching(response)
    url = await validate_service_url(body.url, "Emby URL")
    success = await emby.validate_connection(
        url,
        body.token.get_secret_value(),
        body.user_id,
    )
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Emby or invalid User ID")
    return {"status": "ok"}

@router.post("/test-plex")
async def test_plex(
    body: schemas.ServiceConnectionTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import plex
    _prevent_sensitive_response_caching(response)
    url = await validate_service_url(body.url, "Plex URL")
    success = await plex.validate_connection(url, body.token.get_secret_value())
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Plex")
    return {"status": "ok"}


# ── "Login with Plex" (PIN auth) ──────────────────────────────────────────────

# user_id -> {"pin_id": int, "client_id": str, "created_at": datetime}. Transient;
# a pending PIN only needs to survive the ~2 min the user spends on app.plex.tv.
_PLEX_PIN_CACHE: dict[int, dict] = {}
_PLEX_PIN_TTL = timedelta(minutes=15)


async def _get_plex_client_identifier(db: AsyncSession) -> str:
    """Return this instance's stable X-Plex-Client-Identifier, generating and
    persisting one on first use."""
    gs = (await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))).scalar_one_or_none()
    if gs is None:
        gs = GlobalSettings(id=1)
        db.add(gs)
    if not gs.plex_client_identifier:
        gs.plex_client_identifier = f"scrob-{secrets.token_hex(12)}"
        await db.commit()
    return gs.plex_client_identifier


@router.post("/plex/pin/start")
async def plex_pin_start(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Begin the "Login with Plex" flow. Returns a PIN code, the URL the user
    opens to authorize it, and the poll interval."""
    from core import plex

    client_id = await _get_plex_client_identifier(db)
    try:
        pin = await plex.create_auth_pin(client_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach plex.tv: {exc}")

    _PLEX_PIN_CACHE[current_user.id] = {
        "pin_id": pin["id"],
        "client_id": client_id,
        "created_at": datetime.now(timezone.utc),
    }
    return {
        "pin_id": pin["id"],
        "auth_url": plex.build_auth_url(client_id, pin["code"]),
        "interval": 2,
    }


@router.post("/plex/pin/poll")
async def plex_pin_poll(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check whether the user has authorized the PIN. While pending, returns
    {status: "pending"}. Once claimed, returns the Plex account plus every server
    it can reach, each with a ready-to-use URL and token for the connection form."""
    from core import plex

    entry = _PLEX_PIN_CACHE.get(current_user.id)
    if not entry:
        raise HTTPException(status_code=400, detail="No pending Plex login. Start again.")
    if datetime.now(timezone.utc) - entry["created_at"] > _PLEX_PIN_TTL:
        _PLEX_PIN_CACHE.pop(current_user.id, None)
        raise HTTPException(status_code=400, detail="Plex login expired. Start again.")

    try:
        auth_token = await plex.poll_auth_pin(entry["client_id"], entry["pin_id"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach plex.tv: {exc}")
    if not auth_token:
        return {"status": "pending"}

    _PLEX_PIN_CACHE.pop(current_user.id, None)

    account = await plex.get_account(entry["client_id"], auth_token)
    if not account:
        raise HTTPException(status_code=502, detail="Plex authorized but the account could not be read.")

    try:
        servers = await plex.get_servers(entry["client_id"], auth_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not list Plex servers: {exc}")

    resolved = []
    for server in servers:
        probe = await plex.resolve_connections(server)
        if not probe["connections"]:
            continue
        resolved.append({
            "name": server["name"],
            "machine_identifier": server["machine_identifier"],
            "owned": server["owned"],
            "token": server["access_token"],
            "url": probe["recommended"],
            "connections": probe["connections"],
        })

    return {
        "status": "connected",
        "account": account,
        "auth_token": auth_token,
        "servers": resolved,
    }


@router.post("/stremio/link/start")
async def start_stremio_link(
    current_user: User = Depends(get_current_user),
):
    from core import stremio

    try:
        link = await stremio.create_link_code()
    except stremio.StremioAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "code": link["code"],
        "link": link["link"],
        "qrcode": link["qrcode"],
    }


@router.post("/stremio/link/poll")
async def poll_stremio_link(
    body: schemas.StremioLinkPollRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from core import stremio

    existing_result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.user_id == current_user.id,
            MediaServerConnection.type == "stremio",
        )
    )
    existing = existing_result.scalar_one_or_none()
    # A reconnect passes the id of the (e.g. disconnected) connection it's
    # replacing the auth key on; anything else with an existing row present
    # is the "add new" flow hitting Scrob's one-Stremio-connection limit.
    if existing and existing.id != body.connection_id:
        raise HTTPException(status_code=409, detail="Stremio is already connected")

    try:
        auth_key = await stremio.read_link_code(body.code)
        if auth_key is None:
            return {"status": "pending"}
        account = await stremio.validate_auth_key(auth_key)
    except stremio.StremioAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if existing:
        # Reconnecting: replace the auth key in place and keep the user's
        # existing sync/push settings — this request's defaults only apply
        # to a brand-new connection.
        existing.token = auth_key
        existing.server_user_id = str(account["_id"])
        existing.server_username = str(account.get("email") or "Stremio")
        await db.commit()
        await db.refresh(existing)
        return {
            "status": "connected",
            "connection": schemas.MediaServerConnectionResponse.model_validate(existing),
        }

    connection = MediaServerConnection(
        user_id=current_user.id,
        type="stremio",
        name=body.name.strip() or "Stremio",
        url=stremio.DEFAULT_URL,
        token=auth_key,
        server_user_id=str(account["_id"]),
        server_username=str(account.get("email") or "Stremio"),
        sync_collection=body.sync_collection,
        sync_watched=body.sync_watched,
        sync_ratings=False,
        sync_playback=body.sync_playback,
        push_collection=body.push_collection,
        push_watched=body.push_watched,
        push_ratings=False,
        push_playback=body.push_playback,
        auto_sync_interval=body.auto_sync_interval,
        auto_push_interval=body.auto_push_interval,
    )
    db.add(connection)
    await db.commit()
    await db.refresh(connection)
    return {
        "status": "connected",
        "connection": schemas.MediaServerConnectionResponse.model_validate(connection),
    }


@router.post("/nuvio-login")
async def login_nuvio(
    body: schemas.NuvioLoginRequest,
    current_user: User = Depends(get_current_user),
):
    from core import nuvio

    url = await validate_service_url(body.url, "Nuvio URL")
    try:
        session, profiles = await nuvio.authenticate(url, body.email, body.password)
    except nuvio.NuvioAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "url": url,
        "refresh_token": session.refresh_token,
        "profiles": profiles,
    }


@router.post("/test-nuvio")
async def test_nuvio(
    body: schemas.NuvioConnectionTestRequest,
    current_user: User = Depends(get_current_user),
):
    from core import nuvio

    url = await validate_service_url(body.url, "Nuvio URL")
    profile_id = _parse_nuvio_profile_id(str(body.profile_id))
    try:
        session, profiles = await nuvio.validate_connection(url, body.token, profile_id)
    except nuvio.NuvioAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "ok",
        "message": "Nuvio connection is valid.",
        "refresh_token": session.refresh_token,
        "profiles": profiles,
    }


@router.post("/arvio-login")
async def login_arvio(
    body: schemas.ArvioLoginRequest,
    current_user: User = Depends(get_current_user),
):
    from core import arvio

    url = await validate_service_url(body.url, "ARVIO URL")
    try:
        session, profiles = await arvio.authenticate(url, body.email, body.password, api_key=body.app_key)
    except arvio.ArvioAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "url": url,
        "refresh_token": session.refresh_token,
        "profiles": profiles,
    }


@router.post("/test-arvio")
async def test_arvio(
    body: schemas.ArvioConnectionTestRequest,
    current_user: User = Depends(get_current_user),
):
    from core import arvio

    url = await validate_service_url(body.url, "ARVIO URL")
    try:
        session, profiles = await arvio.validate_connection(url, body.token, body.profile_id, api_key=body.app_key)
    except arvio.ArvioAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "ok",
        "message": "ARVIO connection is valid.",
        "refresh_token": session.refresh_token,
        "profiles": profiles,
    }

@router.post("/test-radarr")
async def test_radarr(
    body: schemas.ServiceConnectionTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import radarr
    _prevent_sensitive_response_caching(response)
    url = await validate_service_url(body.url, "Radarr URL")
    success = await radarr.validate_connection(url, body.token.get_secret_value())
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Radarr")
    return {"status": "ok"}

@router.post("/radarr/profiles")
async def get_radarr_profiles(
    body: schemas.ServiceConnectionTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import radarr
    _prevent_sensitive_response_caching(response)
    url = await validate_service_url(body.url, "Radarr URL")
    token = body.token.get_secret_value()
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
    body: schemas.ServiceConnectionTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import sonarr
    _prevent_sensitive_response_caching(response)
    url = await validate_service_url(body.url, "Sonarr URL")
    success = await sonarr.validate_connection(url, body.token.get_secret_value())
    if not success:
        raise HTTPException(status_code=400, detail="Failed to connect to Sonarr")
    return {"status": "ok"}

@router.get("/connection-status")
async def get_connection_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    import asyncio
    from core import radarr as rdr, sonarr as snr

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    user_settings = settings_result.scalar_one_or_none()

    conns_result = await db.execute(
        select(MediaServerConnection).where(MediaServerConnection.user_id == current_user.id)
    )
    media_server_conns = conns_result.scalars().all()

    async def check_radarr():
        if not user_settings or not (user_settings.radarr_url and user_settings.radarr_token):
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
        if not user_settings or not (user_settings.sonarr_url and user_settings.sonarr_token):
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
        from routers.trakt import TraktTokenError, ensure_valid_trakt_token
        if not user_settings or not (user_settings.trakt_access_token and user_settings.trakt_client_id):
            return {"configured": False, "connected": False}
        try:
            await ensure_valid_trakt_token(db, user_settings, force_check=True)
            return {"configured": True, "connected": True}
        except TraktTokenError:
            return {"configured": True, "connected": False}

    async def check_media_server(conn):
        from core import arvio, jellyfin, nuvio, plex, stremio

        try:
            if conn.type == "plex":
                connected = await plex.validate_connection(conn.url, conn.token)
            elif conn.type == "nuvio":
                profile_id = _parse_nuvio_profile_id(conn.server_user_id)
                # Nuvio's refresh token is single-use; the lock keeps this
                # status check from racing another request (e.g. a second
                # open tab) that reads and redeems the same stale token. The
                # lock alone isn't enough though - conn was loaded before
                # acquiring it, so a request that already rotated the token
                # while this one waited would still leave conn.token stale;
                # re-read it now that the lock is held.
                async with nuvio.connection_lock(conn.id):
                    await db.refresh(conn)
                    session, profiles = await nuvio.validate_connection(conn.url, conn.token, profile_id)
                    conn.token = session.refresh_token
                conn.server_username = _nuvio_profile_name(profiles, profile_id)
                connected = True
            elif conn.type == "arvio":
                profile_id = conn.server_user_id if (conn.server_user_id and conn.server_user_id != "undefined") else None
                # Same single-use rotating refresh token as Nuvio - see the
                # comment on the Nuvio branch above. Re-read conn under the
                # lock before redeeming its token.
                async with arvio.connection_lock(conn.id):
                    await db.refresh(conn)
                    session, profiles = await arvio.validate_connection(conn.url, conn.token, profile_id)
                    conn.token = session.refresh_token
                if profile_id is None and profiles:
                    conn.server_user_id = profiles[0]["id"]
                    await db.commit()
                conn.server_username = arvio.get_profile_name(profiles, conn.server_user_id)
                connected = True
            elif conn.type == "stremio":
                account = await stremio.validate_auth_key(conn.token)
                conn.server_user_id = str(account["_id"])
                conn.server_username = str(account.get("email") or "Stremio")
                connected = True
            else:
                connected = await jellyfin.validate_connection(conn.url, conn.token, conn.server_user_id)
        except Exception:
            connected = False
        result = {"id": conn.id, "connected": connected}
        if conn.type == "nuvio" and connected:
            result["token"] = conn.token
            result["profile_name"] = conn.server_username
            result["profiles"] = [
                {
                    "profile_index": profile.get("profile_index"),
                    "name": str(profile.get("name") or "").strip()
                    or f"Profile {profile.get('profile_index')}",
                }
                for profile in profiles
            ]
        elif conn.type == "arvio" and connected:
            result["token"] = conn.token
            result["profile_name"] = conn.server_username
            result["profiles"] = profiles
        return result

    async def check_simkl():
        from core import simkl as simkl_client
        if not user_settings or not (user_settings.simkl_access_token and user_settings.simkl_client_id):
            return {"configured": False, "connected": False}
        connected = await simkl_client.validate_token(user_settings.simkl_client_id, user_settings.simkl_access_token)
        return {"configured": True, "connected": connected}

    async def check_mdblist():
        from core import mdblist
        if not user_settings or not user_settings.mdblist_api_key:
            return {"configured": False, "connected": False}
        connected = await mdblist.validate_api_key(user_settings.mdblist_api_key)
        return {"configured": True, "connected": connected}

    media_server_tasks = [check_media_server(c) for c in media_server_conns]
    rdr_status, snr_status, trakt_status, simkl_status, mdblist_status, *ms_statuses = await asyncio.gather(
        check_radarr(), check_sonarr(), check_trakt(), check_simkl(), check_mdblist(), *media_server_tasks
    )
    if any(conn.type in ("nuvio", "stremio") for conn in media_server_conns):
        await db.commit()

    return {"radarr": rdr_status, "sonarr": snr_status, "trakt": trakt_status, "simkl": simkl_status, "mdblist": mdblist_status, "connections": ms_statuses}


@router.post("/sonarr/profiles")
async def get_sonarr_profiles(
    body: schemas.ServiceConnectionTestRequest,
    response: Response,
    current_user: User = Depends(get_current_user)
):
    from core import sonarr
    _prevent_sensitive_response_caching(response)
    url = await validate_service_url(body.url, "Sonarr URL")
    token = body.token.get_secret_value()
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


# --- OAuth 2.0 Device Authorization Grant (RFC 8628), #331 -------------------
#
# Lets third-party clients (e.g. the Umbrella Kodi add-on) obtain a
# write-scoped access token without ever handling the user's password: the
# client shows a short code, the user approves it from any logged-in browser
# (so 2FA works untouched), and each grant is independently revocable from
# Connected Apps. The resulting token is scope-limited - dependencies.py
# rejects it on every account/security endpoint (see DEVICE_TOKEN_TYPE).
#
# All timestamps here are naive UTC (datetime.utcnow), matching the
# oauth_device_grants columns, so comparisons never mix aware/naive.

DEVICE_CODE_TTL = timedelta(minutes=15)
DEVICE_ACCESS_TOKEN_TTL = timedelta(hours=24)
DEVICE_POLL_INTERVAL = 5
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
SUPPORTED_DEVICE_SCOPES = {"write"}
# Crockford-ish alphabet: no 0/O/1/I/L to keep the code unambiguous on a TV.
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_user_code() -> str:
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _normalize_user_code(value: str) -> str:
    """Accept the code however the user typed it (lower-case, spaces, missing
    dash) and return the canonical ``XXXX-XXXX`` form, or "" if it can't be
    one."""
    cleaned = "".join(c for c in (value or "").upper() if c in _USER_CODE_ALPHABET)
    if len(cleaned) != 8:
        return ""
    return f"{cleaned[:4]}-{cleaned[4:]}"


def _oauth_error(code: str, http_status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"error": code})


async def _read_token_params(request: Request) -> dict:
    """RFC 8628 mandates application/x-www-form-urlencoded, but accept JSON too
    since that's what the Trakt/Simkl device clients this mirrors tend to
    send."""
    if "application/json" in request.headers.get("content-type", ""):
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    try:
        return {k: str(v) for k, v in (await request.form()).items()}
    except Exception:
        return {}


def _issue_device_access_token(grant: OAuthDeviceGrant) -> dict:
    access = create_access_token(
        subject=grant.user_id,
        expires_delta=DEVICE_ACCESS_TOKEN_TTL,
        extra_claims={"type": DEVICE_TOKEN_TYPE, "scope": grant.scope, "jti": str(grant.id)},
    )
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": int(DEVICE_ACCESS_TOKEN_TTL.total_seconds()),
        "scope": grant.scope,
    }


@router.post("/device/code", response_model=schemas.DeviceCodeResponse)
@limiter.limit("10/minute")
async def device_code(
    request: Request,
    body: schemas.DeviceCodeRequest,
    db: AsyncSession = Depends(get_db),
):
    scope = (body.scope or "write").strip().lower()
    if scope not in SUPPORTED_DEVICE_SCOPES:
        raise HTTPException(status_code=400, detail="Unsupported scope")
    client_name = (body.client_name or "").strip()[:120] or "Unknown app"

    now = datetime.utcnow()
    # Opportunistic housekeeping so the table doesn't accumulate dead rows.
    await db.execute(
        delete(OAuthDeviceGrant).where(
            or_(
                and_(OAuthDeviceGrant.status.in_(("pending", "expired")), OAuthDeviceGrant.expires_at < now),
                and_(OAuthDeviceGrant.status == "denied", OAuthDeviceGrant.created_at < now - timedelta(days=7)),
                and_(OAuthDeviceGrant.revoked_at.isnot(None), OAuthDeviceGrant.revoked_at < now - timedelta(days=30)),
            )
        )
    )

    user_code = ""
    for _ in range(6):
        candidate = _generate_user_code()
        clash = await db.execute(select(OAuthDeviceGrant.id).where(OAuthDeviceGrant.user_code == candidate))
        if clash.scalar_one_or_none() is None:
            user_code = candidate
            break
    if not user_code:
        raise HTTPException(status_code=503, detail="Could not allocate a code, please retry")

    device_code_raw = generate_opaque_token()
    db.add(
        OAuthDeviceGrant(
            device_code_hash=hash_opaque_token(device_code_raw),
            user_code=user_code,
            client_name=client_name,
            scope=scope,
            status="pending",
            interval=DEVICE_POLL_INTERVAL,
            expires_at=now + DEVICE_CODE_TTL,
        )
    )
    await db.commit()

    base = app_settings.server_url.rstrip("/")
    verification_uri = f"{base}/link"
    return schemas.DeviceCodeResponse(
        device_code=device_code_raw,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=f"{verification_uri}?code={user_code}",
        expires_in=int(DEVICE_CODE_TTL.total_seconds()),
        interval=DEVICE_POLL_INTERVAL,
    )


@router.post("/device/token")
@limiter.limit("120/minute")
async def device_token(request: Request, db: AsyncSession = Depends(get_db)):
    params = await _read_token_params(request)
    grant_type = (params.get("grant_type") or "").strip()

    if grant_type in (DEVICE_GRANT_TYPE, "device_code"):
        return await _device_token_exchange(db, params.get("device_code") or "")
    if grant_type == "refresh_token":
        return await _device_token_refresh(db, params.get("refresh_token") or "")
    return _oauth_error("unsupported_grant_type")


async def _device_token_exchange(db: AsyncSession, device_code_raw: str) -> JSONResponse | dict:
    if not device_code_raw:
        return _oauth_error("invalid_request")

    result = await db.execute(
        select(OAuthDeviceGrant).where(
            OAuthDeviceGrant.device_code_hash == hash_opaque_token(device_code_raw)
        )
    )
    grant = result.scalar_one_or_none()
    if grant is None:
        return _oauth_error("invalid_grant")

    now = datetime.utcnow()

    # The device_code is single-use: once tokens have been minted the client
    # must use its refresh token.
    if grant.token_issued_at is not None:
        return _oauth_error("invalid_grant")

    # Enforce the polling interval (RFC 8628 sec. 3.5).
    if grant.last_polled_at is not None and (now - grant.last_polled_at).total_seconds() < grant.interval:
        grant.interval += 5
        grant.last_polled_at = now
        await db.commit()
        return _oauth_error("slow_down")
    grant.last_polled_at = now

    if grant.status in ("pending", "expired") and now >= grant.expires_at:
        grant.status = "expired"
        await db.commit()
        return _oauth_error("expired_token")
    if grant.status == "expired":
        await db.commit()
        return _oauth_error("expired_token")
    if grant.status == "denied":
        await db.commit()
        return _oauth_error("access_denied")
    if grant.status == "pending":
        await db.commit()
        return _oauth_error("authorization_pending")

    # status == "approved"
    refresh_raw = generate_opaque_token()
    grant.refresh_token_hash = hash_opaque_token(refresh_raw)
    grant.prev_refresh_token_hash = None
    grant.token_issued_at = now
    grant.last_seen_at = now
    await db.commit()

    payload = _issue_device_access_token(grant)
    payload["refresh_token"] = refresh_raw
    return payload


async def _device_token_refresh(db: AsyncSession, refresh_raw: str) -> JSONResponse | dict:
    if not refresh_raw:
        return _oauth_error("invalid_request")

    token_hash = hash_opaque_token(refresh_raw)
    result = await db.execute(
        select(OAuthDeviceGrant).where(OAuthDeviceGrant.refresh_token_hash == token_hash)
    )
    grant = result.scalar_one_or_none()

    if grant is None:
        # A refresh token we rotated away is being presented again - the only
        # way that happens is a stolen copy racing the legitimate client.
        # Kill the whole grant so neither party can use it.
        replay = await db.execute(
            select(OAuthDeviceGrant).where(OAuthDeviceGrant.prev_refresh_token_hash == token_hash)
        )
        stolen = replay.scalar_one_or_none()
        if stolen is not None and stolen.revoked_at is None:
            stolen.revoked_at = datetime.utcnow()
            await db.commit()
        return _oauth_error("invalid_grant")

    if grant.revoked_at is not None or grant.status != "approved":
        return _oauth_error("invalid_grant")

    now = datetime.utcnow()
    new_refresh = generate_opaque_token()
    grant.prev_refresh_token_hash = grant.refresh_token_hash
    grant.refresh_token_hash = hash_opaque_token(new_refresh)
    grant.last_seen_at = now
    await db.commit()

    payload = _issue_device_access_token(grant)
    payload["refresh_token"] = new_refresh
    return payload


async def _load_pending_grant(db: AsyncSession, user_code: str) -> OAuthDeviceGrant:
    code = _normalize_user_code(user_code)
    if not code:
        raise HTTPException(status_code=404, detail="Unknown or expired code")
    result = await db.execute(select(OAuthDeviceGrant).where(OAuthDeviceGrant.user_code == code))
    grant = result.scalar_one_or_none()
    if grant is None or grant.status != "pending" or datetime.utcnow() >= grant.expires_at:
        raise HTTPException(status_code=404, detail="Unknown or expired code")
    return grant


@router.get("/device/pending", response_model=schemas.DevicePendingResponse)
@limiter.limit("30/minute")
async def device_pending(
    request: Request,
    user_code: str = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Describe a still-pending grant so the verification page can show the
    user what they're about to authorize. Requires a normal logged-in session
    (which is what makes 2FA a non-issue for the requesting device)."""
    grant = await _load_pending_grant(db, user_code)
    return schemas.DevicePendingResponse(
        client_name=grant.client_name,
        scope=grant.scope,
        requested_at=grant.created_at,
    )


@router.post("/device/approve")
@limiter.limit("20/minute")
async def device_approve(
    request: Request,
    body: schemas.DeviceApproveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    action = (body.action or "").strip().lower()
    if action not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="Invalid action")

    grant = await _load_pending_grant(db, body.user_code)
    grant.user_id = current_user.id
    if action == "approve":
        grant.status = "approved"
        grant.approved_at = datetime.utcnow()
        chosen_name = (body.name or "").strip()[:120]
        if chosen_name:
            grant.client_name = chosen_name
    else:
        grant.status = "denied"
    await db.commit()
    return {"status": grant.status}


@router.get("/device/grants", response_model=list[schemas.DeviceGrantItem])
async def list_device_grants(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(OAuthDeviceGrant)
        .where(
            OAuthDeviceGrant.user_id == current_user.id,
            OAuthDeviceGrant.status == "approved",
            OAuthDeviceGrant.revoked_at.is_(None),
        )
        .order_by(OAuthDeviceGrant.approved_at.desc())
    )
    return result.scalars().all()


@router.delete("/device/grants/{grant_id}")
async def revoke_device_grant(
    grant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(OAuthDeviceGrant).where(
            OAuthDeviceGrant.id == grant_id,
            OAuthDeviceGrant.user_id == current_user.id,
        )
    )
    grant = result.scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=404, detail="Not found")
    if grant.revoked_at is None:
        grant.revoked_at = datetime.utcnow()
        await db.commit()
    return {"status": "revoked"}
