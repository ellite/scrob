from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, case
from sqlalchemy.orm import aliased

from db import get_db
from dependencies import get_current_user, get_optional_user
from models.users import User
from models.profile import UserProfileData, PrivacyLevel
from models.events import WatchEvent
from models.collection import Collection
from models.media import Media
from models.ratings import Rating
from models.show import Show as ShowModel
from models.comments import Comment as CommentModel
from models.lists import List as ListModel, ListItem
from models.follows import Follow
from core.config import settings
import schemas

router = APIRouter()


@router.get("/me", response_model=schemas.UserProfileResponse)
async def get_profile(
    current_user: User = Depends(get_current_user),
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


@router.get("/avatar/{user_id}")
async def get_avatar(user_id: int, db: AsyncSession = Depends(get_db)):
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
    current_user: User = Depends(get_optional_user),
):
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
            | (User.id == (current_user.id if current_user else -1)),
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
        .where(WatchEvent.user_id.in_(user_ids), Media.media_type == "movie")
        .group_by(WatchEvent.user_id)
    )
    movies_map = dict(movies_q.all())

    shows_q = await db.execute(
        select(WatchEvent.user_id, func.count(func.distinct(ShowModel.id)))
        .join(Media, WatchEvent.media_id == Media.id)
        .join(ShowModel, Media.show_id == ShowModel.id)
        .where(WatchEvent.user_id.in_(user_ids))
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


@router.get("/{user_id}", response_model=schemas.PublicProfileResponse)
async def get_public_profile(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    # Fetch user and profile
    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    result = await db.execute(
        select(UserProfileData).where(UserProfileData.user_id == user_id)
    )
    profile = result.scalar_one_or_none()

    # Privacy Check
    is_owner = current_user and current_user.id == user_id
    is_admin = current_user and current_user.role == "admin"
    privacy = profile.privacy_level if profile else PrivacyLevel.private

    is_mutual_follow = False
    if current_user and not is_owner and privacy == PrivacyLevel.friends_only:
        mutual_q = await db.execute(
            select(func.count())
            .select_from(Follow)
            .where(Follow.follower_id == current_user.id, Follow.following_id == user_id)
            .where(
                select(Follow.id)
                .where(Follow.follower_id == user_id, Follow.following_id == current_user.id)
                .exists()
            )
        )
        is_mutual_follow = mutual_q.scalar_one() > 0

    if not (is_owner or is_admin or privacy == PrivacyLevel.public or is_mutual_follow):
        raise HTTPException(status_code=403, detail="This profile is private")

    # --- Stats ---
    watched_q = await db.execute(
        select(func.count(func.distinct(WatchEvent.media_id)))
        .where(WatchEvent.user_id == user_id)
    )
    total_watched = watched_q.scalar_one()

    collected_q = await db.execute(
        select(func.count(func.distinct(Collection.media_id)))
        .where(Collection.user_id == user_id)
    )
    total_collected = collected_q.scalar_one()

    movies_q = await db.execute(
        select(func.count(func.distinct(WatchEvent.media_id)))
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type == "movie")
    )
    movies_watched = movies_q.scalar_one()

    shows_q = await db.execute(
        select(func.count(func.distinct(ShowModel.id)))
        .join(Media, Media.show_id == ShowModel.id)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id)
    )
    shows_watched = shows_q.scalar_one()

    rated_q = await db.execute(
        select(func.count(Rating.id))
        .where(Rating.user_id == user_id, Rating.rating.isnot(None))
    )
    total_rated = rated_q.scalar_one()

    # --- Recently Watched Movies ---
    rw_movies_q = await db.execute(
        select(WatchEvent, Media)
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type == "movie")
        .order_by(WatchEvent.watched_at.desc())
        .limit(12)
    )
    recently_watched_movies = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "movie",
            "title": media.title,
            "poster_path": media.poster_path,
            "watched_at": we.watched_at.isoformat(),
        }
        for we, media in rw_movies_q.all()
    ]

    # --- Recently Watched Shows (episodes) ---
    rw_shows_q = await db.execute(
        select(WatchEvent, Media, ShowModel)
        .join(Media, WatchEvent.media_id == Media.id)
        .outerjoin(ShowModel, Media.show_id == ShowModel.id)
        .where(WatchEvent.user_id == user_id, Media.media_type == "episode")
        .order_by(WatchEvent.watched_at.desc())
        .limit(12)
    )
    recently_watched_shows = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "episode",
            "title": media.title,
            "backdrop_path": show.backdrop_path if show else media.backdrop_path,
            "poster_path": show.poster_path if show else media.poster_path,
            "watched_at": we.watched_at.isoformat(),
            "show_title": show.title if show else None,
            "show_tmdb_id": show.tmdb_id if show else None,
            "season_number": media.season_number,
            "episode_number": media.episode_number,
        }
        for we, media, show in rw_shows_q.all()
    ]

    # --- Top Rated Movies ---
    tr_movies_q = await db.execute(
        select(Rating, Media)
        .join(Media, Rating.media_id == Media.id)
        .where(
            Rating.user_id == user_id,
            Rating.season_number.is_(None),
            Media.media_type == "movie",
            Rating.rating.isnot(None),
        )
        .order_by(Rating.rating.desc())
        .limit(16)
    )
    top_rated_movies = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "movie",
            "title": media.title,
            "poster_path": media.poster_path,
            "user_rating": rating.rating,
        }
        for rating, media in tr_movies_q.all()
    ]

    # --- Top Rated Shows ---
    tr_shows_q = await db.execute(
        select(Rating, Media, ShowModel)
        .join(Media, Rating.media_id == Media.id)
        .outerjoin(ShowModel, (Media.media_type == "series") & (Media.tmdb_id == ShowModel.tmdb_id))
        .where(
            Rating.user_id == user_id,
            Rating.season_number.is_(None),
            Media.media_type == "series",
            Rating.rating.isnot(None),
        )
        .order_by(Rating.rating.desc())
        .limit(16)
    )
    top_rated_shows = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "series",
            "title": media.title,
            "poster_path": show.poster_path if show else media.poster_path,
            "user_rating": rating.rating,
        }
        for rating, media, show in tr_shows_q.all()
    ]

    # --- Recent Comments ---
    recent_comments_q = await db.execute(
        select(CommentModel)
        .where(CommentModel.user_id == user_id)
        .order_by(CommentModel.created_at.desc())
        .limit(5)
    )
    comments_list = recent_comments_q.scalars().all()

    # Batch resolve titles for comments
    show_tmdb_ids = list({c.tmdb_id for c in comments_list if c.media_type in ("series", "season", "episode")})
    movie_tmdb_ids = list({c.tmdb_id for c in comments_list if c.media_type == "movie"})

    show_titles: dict[int, tuple[str, str | None]] = {}
    movie_titles: dict[int, tuple[str, str | None]] = {}

    if show_tmdb_ids:
        sq = await db.execute(
            select(ShowModel.tmdb_id, ShowModel.title, ShowModel.poster_path)
            .where(ShowModel.tmdb_id.in_(show_tmdb_ids))
        )
        for tmdb_id, title, poster_path in sq.all():
            show_titles[tmdb_id] = (title, poster_path)

    if movie_tmdb_ids:
        mq = await db.execute(
            select(Media.tmdb_id, Media.title, Media.poster_path)
            .where(Media.tmdb_id.in_(movie_tmdb_ids), Media.media_type == "movie")
            .group_by(Media.tmdb_id, Media.title, Media.poster_path)
        )
        for tmdb_id, title, poster_path in mq.all():
            movie_titles[tmdb_id] = (title, poster_path)

    recent_comments = []
    for c in comments_list:
        if c.media_type in ("series", "season", "episode"):
            info = show_titles.get(c.tmdb_id)
        else:
            info = movie_titles.get(c.tmdb_id)
        recent_comments.append({
            "id": c.id,
            "content": c.content,
            "media_type": c.media_type,
            "tmdb_id": c.tmdb_id,
            "season_number": c.season_number,
            "episode_number": c.episode_number,
            "title": info[0] if info else None,
            "poster_path": info[1] if info else None,
            "created_at": c.created_at.isoformat(),
        })

    # --- Followers / Following ---
    follower_count_q = await db.execute(
        select(func.count(Follow.id)).where(Follow.following_id == user_id)
    )
    follower_count = follower_count_q.scalar_one()

    following_count_q = await db.execute(
        select(func.count(Follow.id)).where(Follow.follower_id == user_id)
    )
    following_count = following_count_q.scalar_one()

    # Preview: up to 8 of each, with display_name and avatar
    followers_q = await db.execute(
        select(User, UserProfileData)
        .join(Follow, Follow.follower_id == User.id)
        .outerjoin(UserProfileData, UserProfileData.user_id == User.id)
        .where(Follow.following_id == user_id)
        .order_by(Follow.created_at.desc())
        .limit(8)
    )
    followers_preview = [
        {
            "id": u.id,
            "display_name": p.display_name if p and p.display_name else u.username,
            "avatar_url": f"/profile/avatar/{u.id}" if (p and p.avatar_path) else None,
        }
        for u, p in followers_q.all()
    ]

    following_q = await db.execute(
        select(User, UserProfileData)
        .join(Follow, Follow.following_id == User.id)
        .outerjoin(UserProfileData, UserProfileData.user_id == User.id)
        .where(Follow.follower_id == user_id)
        .order_by(Follow.created_at.desc())
        .limit(8)
    )
    following_preview = [
        {
            "id": u.id,
            "display_name": p.display_name if p and p.display_name else u.username,
            "avatar_url": f"/profile/avatar/{u.id}" if (p and p.avatar_path) else None,
        }
        for u, p in following_q.all()
    ]

    is_following = False
    if current_user and current_user.id != user_id:
        follow_check = await db.execute(
            select(Follow).where(Follow.follower_id == current_user.id, Follow.following_id == user_id)
        )
        is_following = follow_check.scalar_one_or_none() is not None

    # --- Lists ---
    lists_query = (
        select(
            ListModel.id,
            ListModel.name,
            ListModel.description,
            ListModel.privacy_level,
            ListModel.updated_at,
            func.count(ListModel.items).label("item_count"),
        )
        .outerjoin(ListModel.items)
        .where(ListModel.user_id == user_id)
        .group_by(ListModel.id)
        .order_by(ListModel.updated_at.desc())
    )
    if not (is_owner or is_admin):
        lists_query = lists_query.where(ListModel.privacy_level == PrivacyLevel.public)

    lists_result = await db.execute(lists_query)
    lists_rows = lists_result.all()
    list_ids = [row.id for row in lists_rows]

    # Fetch up to 3 preview posters per list using ROW_NUMBER
    posters_by_list: dict[int, list[str]] = {}
    if list_ids:
        ShowAlias = aliased(ShowModel)
        rn = func.row_number().over(
            partition_by=ListItem.list_id,
            order_by=ListItem.added_at,
        ).label("rn")
        poster_col = case(
            (Media.poster_path.isnot(None), Media.poster_path),
            else_=ShowAlias.poster_path,
        ).label("poster")
        inner = (
            select(ListItem.list_id, poster_col, rn)
            .join(Media, ListItem.media_id == Media.id)
            .outerjoin(ShowAlias, ShowAlias.tmdb_id == Media.tmdb_id)
            .where(ListItem.list_id.in_(list_ids))
        ).subquery()
        posters_q = await db.execute(
            select(inner.c.list_id, inner.c.poster)
            .where(inner.c.rn <= 3)
            .where(inner.c.poster.isnot(None))
        )
        for row in posters_q.all():
            posters_by_list.setdefault(row.list_id, []).append(row.poster)

    user_lists = [
        {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "privacy_level": row.privacy_level.value,
            "item_count": row.item_count,
            "updated_at": row.updated_at.isoformat(),
            "preview_posters": posters_by_list.get(row.id, []),
        }
        for row in lists_rows
    ]

    # Compute display_name from the already-loaded profile to avoid lazy-load in async context
    display_name = (profile.display_name if profile and profile.display_name else user.username)

    return {
        "id": user.id,
        "username": user.username,
        "display_name": display_name,
        "avatar_url": f"/profile/avatar/{user.id}" if (profile and profile.avatar_path) else None,
        "bio": profile.bio if profile else None,
        "country": profile.country if profile else None,
        "movie_genres": profile.movie_genres if profile else [],
        "show_genres": profile.show_genres if profile else [],
        "created_at": user.created_at,
        "total_watched": total_watched,
        "total_collected": total_collected,
        "movies_watched": movies_watched,
        "shows_watched": shows_watched,
        "total_rated": total_rated,
        "recently_watched_movies": recently_watched_movies,
        "recently_watched_shows": recently_watched_shows,
        "top_rated_movies": top_rated_movies,
        "top_rated_shows": top_rated_shows,
        "recent_comments": recent_comments,
        "lists": user_lists,
        "follower_count": follower_count,
        "following_count": following_count,
        "followers": followers_preview,
        "following": following_preview,
        "is_following": is_following,
    }
