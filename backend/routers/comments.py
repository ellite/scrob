from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sa_delete, or_
from sqlalchemy.orm import joinedload
from pydantic import BaseModel

from db import get_db
from models.comments import Comment
from models.users import User
from models.profile import UserProfileData, PrivacyLevel
from models.global_settings import GlobalSettings
from dependencies import get_current_user, get_optional_user

router = APIRouter()

class CommentCreate(BaseModel):
    media_type: str
    tmdb_id: int
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    content: str
    is_spoiler: bool = False

class CommentUpdate(BaseModel):
    content: str
    is_spoiler: bool = False

class CommentResponse(BaseModel):
    id: int
    user_id: int
    username: str
    display_name: str
    user_is_public: bool
    content: str
    created_at: str
    updated_at: Optional[str] = None

@router.get("")
async def list_comments(
    media_type: str,
    tmdb_id: int,
    season_number: Optional[int] = None,
    episode_number: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    query = (
        select(Comment)
        .join(User, Comment.user_id == User.id)
        .outerjoin(UserProfileData, User.id == UserProfileData.user_id)
        .options(joinedload(Comment.user).joinedload(User.profile))
        .where(
            Comment.media_type == media_type,
            Comment.tmdb_id == tmdb_id,
            Comment.season_number == season_number,
            Comment.episode_number == episode_number,
        )
    )

    # Privacy filtering:
    # 1. Profile is public
    # 2. OR owner is the current user
    # 3. OR current user is admin
    # Note: If no profile exists, it's considered private (default).
    if current_user:
        query = query.where(
            or_(
                UserProfileData.privacy_level == PrivacyLevel.public,
                Comment.user_id == current_user.id,
                current_user.role == "admin",
            )
        )
    else:
        query = query.where(UserProfileData.privacy_level == PrivacyLevel.public)

    query = query.order_by(Comment.created_at.desc())
    
    result = await db.execute(query)
    comments = result.scalars().all()
    
    return [
        {
            "id": c.id,
            "user_id": c.user_id,
            "username": c.user.username,
            "display_name": c.user.display_name,
            "avatar_url": f"/profile/avatar/{c.user_id}" if (c.user.profile and c.user.profile.avatar_path) else None,
            "user_is_public": c.user.profile.privacy_level == PrivacyLevel.public if c.user.profile else False,
            "content": c.content,
            "is_spoiler": c.is_spoiler,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in comments
    ]

@router.post("")
async def create_comment(
    body: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Comment content cannot be empty")

    # The UI already hides the whole section when disabled (#301) - this is
    # just so a direct API call can't post one anyway.
    gs = (await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))).scalar_one_or_none()
    if gs and gs.disable_comments:
        raise HTTPException(status_code=403, detail="Comments are disabled on this server")

    comment = Comment(
        user_id=current_user.id,
        media_type=body.media_type,
        tmdb_id=body.tmdb_id,
        season_number=body.season_number,
        episode_number=body.episode_number,
        content=body.content,
        is_spoiler=body.is_spoiler,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)

    return {
        "id": comment.id,
        "user_id": comment.user_id,
        "username": current_user.username,
        "display_name": current_user.display_name,
        "avatar_url": f"/profile/avatar/{current_user.id}" if (current_user.profile and current_user.profile.avatar_path) else None,
        "user_is_public": current_user.profile.privacy_level == PrivacyLevel.public if current_user.profile else False,
        "content": comment.content,
        "is_spoiler": comment.is_spoiler,
        "created_at": comment.created_at.isoformat(),
    }

@router.patch("/{comment_id}")
async def update_comment(
    comment_id: int,
    body: CommentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()

    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this comment")

    stripped = body.content.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="Comment content cannot be empty")

    comment.content = stripped
    comment.is_spoiler = body.is_spoiler
    await db.commit()
    await db.refresh(comment)

    return {
        "id": comment.id,
        "content": comment.content,
        "is_spoiler": comment.is_spoiler,
        "updated_at": comment.updated_at.isoformat() if comment.updated_at else None,
    }

@router.delete("/{comment_id}")
async def delete_comment(
    comment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Comment).where(Comment.id == comment_id)
    result = await db.execute(query)
    comment = result.scalar_one_or_none()
    
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
        
    if comment.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")
        
    await db.execute(sa_delete(Comment).where(Comment.id == comment_id))
    await db.commit()
    
    return {"message": "Comment deleted"}
