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
from models.global_settings import GlobalSettings
from models.connections import MediaServerConnection
from models.scrobble_connection import ScrobbleConnection
from models.email_activation import EmailActivation
from models.password_reset import PasswordResetToken
from core.security import verify_password, get_password_hash, create_access_token, ALGORITHM
from core.config import settings as app_settings
from core.email import send_activation_email, send_password_reset_email
from core.url_validator import validate_service_url
from core.limiter import limiter
from core.backup import restore_backup
import schemas
from dependencies import get_current_user
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
def _parse_nuvio_profile_id(value: str | None) -> int:
    try:
        profile_id = int(value or "")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Nuvio profile must be an integer from 1 to 6")
    if profile_id < 1 or profile_id > 6:
        raise HTTPException(status_code=400, detail="Nuvio profile must be an integer from 1 to 6")
    return profile_id


def _nuvio_profile_name(profiles: list[dict], profile_id: int) -> str:
    for profile in profiles:
        try:
            matches = int(profile.get("profile_index", 0)) == profile_id
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
        role=user_in.role,
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
    gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = gs_result.scalar_one_or_none()
    data.has_global_tmdb_key = bool(gs and gs.tmdb_api_key)
    data.has_effective_tmdb_key = bool(settings.tmdb_api_key) or data.has_global_tmdb_key
    data.has_global_tvdb_key = bool(gs and gs.tvdb_api_key)
    data.has_effective_tvdb_key = bool(settings.tvdb_api_key) or data.has_global_tvdb_key
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
    READ_ONLY_FIELDS = {"trakt_connected", "simkl_connected", "has_global_tmdb_key", "has_effective_tmdb_key", "has_global_tvdb_key", "has_effective_tvdb_key"}
    update_data = {k: v for k, v in settings_in.model_dump(exclude_unset=True).items() if k not in READ_ONLY_FIELDS}

    if "tmdb_api_key" in update_data and update_data["tmdb_api_key"]:
        success = await tmdb.validate_api_key(update_data["tmdb_api_key"])
        if not success:
            raise HTTPException(status_code=400, detail="Invalid TMDB API Key")

    if "tvdb_api_key" in update_data and update_data["tvdb_api_key"]:
        from core import tvdb
        success = await tvdb.validate_api_key(update_data["tvdb_api_key"])
        if not success:
            raise HTTPException(status_code=400, detail="Invalid TVDB API Key")

    url_fields = {"radarr_url": "Radarr URL", "sonarr_url": "Sonarr URL"}
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
    if body.type not in ("plex", "jellyfin", "emby", "nuvio"):
        raise HTTPException(status_code=400, detail="type must be plex, jellyfin, emby, or nuvio")
    validated_url = await validate_service_url(body.url, f"{body.type.capitalize()} URL")
    connection_token = body.token
    server_user_id = body.server_user_id
    server_username = body.server_username
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

    conn = MediaServerConnection(
        user_id=current_user.id,
        type=body.type,
        name=body.name,
        url=validated_url,
        token=connection_token,
        server_user_id=server_user_id,
        server_username=server_username,
        sync_collection=body.sync_collection,
        sync_watched=body.sync_watched,
        sync_ratings=body.sync_ratings if body.type != "nuvio" else False,
        sync_playback=body.sync_playback,
        push_watched=body.push_watched,
        push_playback=body.push_playback if body.type == "nuvio" else False,
        push_ratings=body.push_ratings if body.type != "nuvio" else False,
        auto_sync_interval=body.auto_sync_interval,
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
    if "url" in update_data and update_data["url"]:
        update_data["url"] = await validate_service_url(update_data["url"], f"{conn.type.capitalize()} URL")

    if conn.type == "nuvio":
        from core import nuvio

        candidate_url = update_data.get("url", conn.url)
        candidate_token = update_data.get("token", conn.token)
        profile_id = _parse_nuvio_profile_id(update_data.get("server_user_id", conn.server_user_id))
        try:
            session, profiles = await nuvio.validate_connection(candidate_url, candidate_token, profile_id)
        except nuvio.NuvioAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        update_data["token"] = session.refresh_token
        update_data["server_user_id"] = str(profile_id)
        update_data["server_username"] = _nuvio_profile_name(profiles, profile_id)
        update_data["sync_ratings"] = False
        update_data["push_ratings"] = False
        update_data["push_playback"] = update_data.get("push_playback", conn.push_playback)
    else:
        update_data["push_playback"] = False

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
    key: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    from core import tmdb
    success = await tmdb.validate_api_key(key)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid TMDB API Key")
    return {"status": "ok", "message": "TMDB API key is valid."}

@router.post("/test-tvdb")
async def test_tvdb(
    key: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    from core import tvdb
    success = await tvdb.validate_api_key(key)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid TVDB API Key")
    return {"status": "ok", "message": "TVDB API key is valid."}

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
        from core import trakt as trakt_client
        from datetime import datetime, timezone
        if not user_settings or not (user_settings.trakt_access_token and user_settings.trakt_client_id):
            return {"configured": False, "connected": False}
        connected = await trakt_client.validate_token(user_settings.trakt_client_id, user_settings.trakt_access_token)
        if not connected and user_settings.trakt_refresh_token and user_settings.trakt_client_secret:
            try:
                token_data = await trakt_client.refresh_access_token(
                    user_settings.trakt_client_id,
                    user_settings.trakt_client_secret,
                    user_settings.trakt_refresh_token,
                )
                user_settings.trakt_access_token = token_data["access_token"]
                user_settings.trakt_refresh_token = token_data["refresh_token"]
                user_settings.trakt_token_expires_at = token_data.get("expires_in", 0) + int(datetime.now(timezone.utc).timestamp())
                await db.commit()
                connected = True
            except Exception:
                pass
        return {"configured": True, "connected": connected}

    async def check_media_server(conn):
        from core import jellyfin, nuvio, plex

        try:
            if conn.type == "plex":
                connected = await plex.validate_connection(conn.url, conn.token)
            elif conn.type == "nuvio":
                profile_id = _parse_nuvio_profile_id(conn.server_user_id)
                session, profiles = await nuvio.validate_connection(conn.url, conn.token, profile_id)
                conn.token = session.refresh_token
                conn.server_username = _nuvio_profile_name(profiles, profile_id)
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
        return result

    async def check_simkl():
        from core import simkl as simkl_client
        if not user_settings or not (user_settings.simkl_access_token and user_settings.simkl_client_id):
            return {"configured": False, "connected": False}
        connected = await simkl_client.validate_token(user_settings.simkl_client_id, user_settings.simkl_access_token)
        return {"configured": True, "connected": connected}

    media_server_tasks = [check_media_server(c) for c in media_server_conns]
    rdr_status, snr_status, trakt_status, simkl_status, *ms_statuses = await asyncio.gather(
        check_radarr(), check_sonarr(), check_trakt(), check_simkl(), *media_server_tasks
    )
    if any(conn.type == "nuvio" for conn in media_server_conns):
        await db.commit()

    return {"radarr": rdr_status, "sonarr": snr_status, "trakt": trakt_status, "simkl": simkl_status, "connections": ms_statuses}


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
