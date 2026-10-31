from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from db import get_db
from dependencies import get_current_user, get_current_user_or_api_key
from models.users import User
from models.profile import UserProfileData
from models.global_settings import GlobalSettings
from core.config import settings
import schemas


router = APIRouter()


@router.get("/public-access-status")
async def get_public_access_status(db: AsyncSession = Depends(get_db)):
    """Unauthenticated: global feature flags every page render needs to know
    about, regardless of login state. Originally just anon-nav access status
    (read by the frontend's auth middleware to decide whether an anonymous
    request to a gated page should be let through) - now also carries other
    admin toggles like disable_comments (#301) that hide UI for every
    visitor, not just anonymous ones."""
    result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = result.scalar_one_or_none()
    return {
        # Requires a global TMDB key even if the toggle is on, in case the key
        # was removed after the admin enabled it.
        "enable_logged_out_navigation": bool(gs and gs.enable_logged_out_navigation and gs.tmdb_api_key),
        "disable_comments": bool(gs and gs.disable_comments),
        # Lets an anonymous visitor's search page offer the TVDB fallback
        # search too, the same as a logged-in user with an effective key -
        # there's no per-anon-visitor UserSettings override to also check.
        "has_global_tvdb_key": bool(gs and gs.tvdb_api_key),
    }


@router.get("/me", response_model=schemas.UserProfileResponse)
async def get_profile(
    current_user: User = Depends(get_current_user_or_api_key),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserProfileData).where(UserProfileData.user_id == current_user.id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        return schemas.UserProfileResponse()
    resp = schemas.UserProfileResponse.model_validate(profile)
    if profile.avatar_path:
        resp.avatar_url = f"/profile/avatar/{current_user.id}"
    return resp


@router.patch("/me", response_model=schemas.UserProfileResponse)
async def update_profile(
    body: schemas.UserProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserProfileData).where(UserProfileData.user_id == current_user.id)
    )
    profile = result.scalar_one_or_none()

    if profile is None:
        profile = UserProfileData(user_id=current_user.id)
        db.add(profile)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)

    await db.commit()
    await db.refresh(profile)
    return profile


_ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}


_AVATAR_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


_MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if file.content_type not in _ALLOWED_AVATAR_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported image type. Use JPEG, PNG or WebP.")

    content = await file.read()
    if len(content) > _MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 5 MB.")

    ext = _AVATAR_EXT[file.content_type]
    avatars_dir = settings.data_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)

    # Remove any existing avatar for this user (may have different extension)
    for old in avatars_dir.glob(f"{current_user.id}.*"):
        old.unlink(missing_ok=True)

    fname = f"{current_user.id}.{ext}"
    (avatars_dir / fname).write_bytes(content)

    result = await db.execute(select(UserProfileData).where(UserProfileData.user_id == current_user.id))
    profile = result.scalar_one_or_none()
    if profile is None:
        profile = UserProfileData(user_id=current_user.id)
        db.add(profile)
    profile.avatar_path = fname
    await db.commit()

    return {"avatar_url": f"/profile/avatar/{current_user.id}"}


@router.delete("/me/avatar")
async def delete_avatar(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(UserProfileData).where(UserProfileData.user_id == current_user.id))
    profile = result.scalar_one_or_none()
    if profile and profile.avatar_path:
        avatars_dir = settings.data_dir / "avatars"
        for old in avatars_dir.glob(f"{current_user.id}.*"):
            old.unlink(missing_ok=True)
        profile.avatar_path = None
        await db.commit()
    return {"status": "ok"}
