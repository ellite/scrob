from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from typing import Optional
from db import get_db
from dependencies import get_current_user, get_current_user_or_api_key, get_optional_user_or_api_key
from models.users import User
from models.profile import UserProfileData, PrivacyLevel
from models.events import WatchEvent
from models.collection import Collection
from models.media import Media
from models.ratings import Rating
from models.show import Show as ShowModel
from models.follows import Follow
from core.config import settings

from .common import _check_profile_access


router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────

@router.get("/avatar/{user_id}")
async def get_avatar(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user_or_api_key),
):
    await _check_profile_access(user_id, current_user, db)

    result = await db.execute(select(UserProfileData).where(UserProfileData.user_id == user_id))
    profile = result.scalar_one_or_none()
    if not profile or not profile.avatar_path:
        raise HTTPException(status_code=404, detail="No avatar")

    path = settings.data_dir / "avatars" / profile.avatar_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Avatar file not found")

    ext = path.suffix.lstrip(".")
    media_type = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    return FileResponse(str(path), media_type=media_type, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/search")
async def search_users(
    q: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Unlike viewing a single profile/list by ID, this is a directory-style
    enumeration of every public/friends_only user matching a pattern, so it
    stays behind real authentication regardless of the enable_logged_out_navigation
    toggle, there's no anonymous "browse all public users" use case to support."""
    if len(q.strip()) < 1:
        return {"results": []}

    pattern = f"%{q.strip()}%"

    # Match on username or display_name, only public profiles (+ own profile)
    users_q = await db.execute(
        select(User, UserProfileData)
        .outerjoin(UserProfileData, UserProfileData.user_id == User.id)
        .where(
            (User.username.ilike(pattern)) | (UserProfileData.display_name.ilike(pattern)),
            (UserProfileData.privacy_level.in_([PrivacyLevel.public, PrivacyLevel.friends_only]))
            | (User.id == current_user.id),
        )
        .order_by(User.username)
        .limit(24)
    )
    rows = users_q.all()
    if not rows:
        return {"results": []}

    user_ids = [u.id for u, _ in rows]

    # Batch stats
    movies_q = await db.execute(
        select(WatchEvent.user_id, func.count(func.distinct(WatchEvent.media_id)))
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id.in_(user_ids), WatchEvent.completed == True, Media.media_type == "movie")
        .group_by(WatchEvent.user_id)
    )
    movies_map = dict(movies_q.all())

    shows_q = await db.execute(
        select(WatchEvent.user_id, func.count(func.distinct(ShowModel.id)))
        .join(Media, WatchEvent.media_id == Media.id)
        .join(ShowModel, Media.show_id == ShowModel.id)
        .where(WatchEvent.user_id.in_(user_ids), WatchEvent.completed == True)
        .group_by(WatchEvent.user_id)
    )
    shows_map = dict(shows_q.all())

    collected_q = await db.execute(
        select(Collection.user_id, func.count(func.distinct(Collection.media_id)))
        .where(Collection.user_id.in_(user_ids))
        .group_by(Collection.user_id)
    )
    collected_map = dict(collected_q.all())

    rated_q = await db.execute(
        select(Rating.user_id, func.count(Rating.id))
        .where(Rating.user_id.in_(user_ids), Rating.rating.isnot(None))
        .group_by(Rating.user_id)
    )
    rated_map = dict(rated_q.all())

    followers_q = await db.execute(
        select(Follow.following_id, func.count(Follow.id))
        .where(Follow.following_id.in_(user_ids))
        .group_by(Follow.following_id)
    )
    followers_map = dict(followers_q.all())

    # Which of these users is the current viewer already following?
    following_set: set[int] = set()
    if current_user:
        fol_q = await db.execute(
            select(Follow.following_id)
            .where(Follow.follower_id == current_user.id, Follow.following_id.in_(user_ids))
        )
        following_set = {row[0] for row in fol_q.all()}

    results = []
    for u, p in rows:
        display_name = p.display_name if p and p.display_name else u.username
        results.append({
            "id": u.id,
            "username": u.username,
            "display_name": display_name,
            "avatar_url": f"/profile/avatar/{u.id}" if (p and p.avatar_path) else None,
            "country": p.country if p else None,
            "movies_watched": movies_map.get(u.id, 0),
            "shows_watched": shows_map.get(u.id, 0),
            "total_collected": collected_map.get(u.id, 0),
            "total_rated": rated_map.get(u.id, 0),
            "follower_count": followers_map.get(u.id, 0),
            "is_following": u.id in following_set,
            "is_self": current_user is not None and current_user.id == u.id,
        })

    return {"results": results}


@router.post("/{user_id}/follow")
async def follow_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself.")
    target = await db.execute(select(User).where(User.id == user_id))
    if not target.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found.")
    existing = await db.execute(
        select(Follow).where(Follow.follower_id == current_user.id, Follow.following_id == user_id)
    )
    if not existing.scalar_one_or_none():
        db.add(Follow(follower_id=current_user.id, following_id=user_id))
        await db.commit()
    return {"status": "following"}


@router.delete("/{user_id}/follow")
async def unfollow_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(Follow).where(Follow.follower_id == current_user.id, Follow.following_id == user_id)
    )
    await db.commit()
    return {"status": "unfollowed"}
