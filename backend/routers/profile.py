import asyncio
from collections import defaultdict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, case, or_
from sqlalchemy.orm import aliased
from datetime import date as DateType, timedelta as TimeDelta
from typing import Optional

from db import get_db, AsyncSessionLocal
from core import tmdb as tmdb_client
from core.credits import credits_stats, maybe_backfill_credits
from core.translations import upsert_media_translation, upsert_show_translation

from dependencies import get_current_user, get_current_user_or_api_key, get_optional_user_or_api_key
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
from models.global_settings import GlobalSettings
from core.config import settings
import schemas

router = APIRouter()


@router.get("/public-access-status")
async def get_public_access_status(db: AsyncSession = Depends(get_db)):
    """Unauthenticated: whether the admin allows anonymous browsing/viewing.
    Read by the frontend's auth middleware to decide whether an anonymous
    request to a gated page should be let through."""
    result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = result.scalar_one_or_none()
    return {
        # Requires a global TMDB key even if the toggle is on, in case the key
        # was removed after the admin enabled it.
        "enable_logged_out_navigation": bool(gs and gs.enable_logged_out_navigation and gs.tmdb_api_key),
    }


async def _check_profile_access(user_id: int, current_user, db: AsyncSession):
    """Returns (user, profile) or raises 404/403."""
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    profile_result = await db.execute(select(UserProfileData).where(UserProfileData.user_id == user_id))
    profile = profile_result.scalar_one_or_none()

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

    # A request with no valid session (no JWT, or an API key that doesn't match
    # any user) reaches here as current_user=None and only gets this far because
    # privacy is public. Gate that specific case on the admin's global toggle too,
    # server-side, rather than trusting the frontend's page-level gate alone,
    # since that gate lives in middleware and can be bypassed by any request
    # carrying an api_key query param (even an invalid one), which the proxy
    # treats as "public route" before ever checking this setting.
    if not current_user and privacy == PrivacyLevel.public:
        gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        gs = gs_result.scalar_one_or_none()
        if not (gs and gs.enable_logged_out_navigation):
            raise HTTPException(status_code=403, detail="This profile is private")

    return user, profile


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


# ── Translation backfill ──────────────────────────────────────────────────────

_TRANSLATION_BACKFILL: dict[int, dict] = {}


@router.post("/me/translations/backfill")
async def start_translation_backfill(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user.id
    if _TRANSLATION_BACKFILL.get(user_id, {}).get("running"):
        raise HTTPException(status_code=409, detail="Backfill already running")

    profile_result = await db.execute(select(UserProfileData).where(UserProfileData.user_id == user_id))
    profile = profile_result.scalar_one_or_none()
    if not profile or not profile.metadata_language:
        raise HTTPException(status_code=400, detail="No metadata language set")

    language = profile.metadata_language

    from routers.media import get_user_tmdb_key
    tmdb_key = await get_user_tmdb_key(db, user_id)
    if not tmdb_key:
        raise HTTPException(status_code=400, detail="TMDB API key not configured")

    _TRANSLATION_BACKFILL[user_id] = {
        "running": True, "progress": 0, "total": 0, "done": False, "error": None,
    }
    background_tasks.add_task(_run_translation_backfill, user_id, language, tmdb_key)
    return {"status": "started"}


@router.get("/me/translations/backfill/status")
async def get_backfill_status(current_user: User = Depends(get_current_user_or_api_key)):
    state = _TRANSLATION_BACKFILL.get(current_user.id)
    if not state:
        return {"running": False, "progress": 0, "total": 0, "done": False, "error": None}
    return state


@router.delete("/me/translations/backfill")
async def abort_backfill(current_user: User = Depends(get_current_user)):
    state = _TRANSLATION_BACKFILL.get(current_user.id)
    if state and state.get("running"):
        state["abort"] = True
    return {"status": "ok"}


async def _run_translation_backfill(user_id: int, language: str, tmdb_key: str) -> None:
    state = _TRANSLATION_BACKFILL[user_id]
    try:
        # ── 1. Read all user-linked items from DB ─────────────────────────────
        async with AsyncSessionLocal() as db:
            has_items_filter = or_(
                Media.id.in_(select(Collection.media_id).where(Collection.user_id == user_id)),
                Media.id.in_(
                    select(WatchEvent.media_id).where(
                        WatchEvent.user_id == user_id, WatchEvent.completed == True
                    )
                ),
                # Items that only live in one of the user's lists (never
                # collected or watched) render on the list pages too, so they
                # need translations as well (#235 follow-up).
                Media.id.in_(
                    select(ListItem.media_id)
                    .join(ListModel, ListModel.id == ListItem.list_id)
                    .where(ListModel.user_id == user_id)
                ),
            )

            movie_q = await db.execute(
                select(Media.id, Media.tmdb_id)
                .where(Media.media_type == "movie", Media.tmdb_id.isnot(None), has_items_filter)
                .distinct()
            )
            movies = movie_q.all()

            show_q = await db.execute(
                select(ShowModel.id, ShowModel.tmdb_id)
                .join(Media, Media.show_id == ShowModel.id)
                .where(Media.media_type == "episode", ShowModel.tmdb_id.isnot(None), has_items_filter)
                .distinct()
            )
            shows = show_q.all()

            ep_q = await db.execute(
                select(Media.id, Media.episode_number, Media.season_number, ShowModel.tmdb_id.label("show_tmdb_id"))
                .join(ShowModel, Media.show_id == ShowModel.id)
                .where(
                    Media.media_type == "episode",
                    Media.season_number.isnot(None),
                    Media.episode_number.isnot(None),
                    ShowModel.tmdb_id.isnot(None),
                    has_items_filter,
                )
                .distinct()
            )
            eps = ep_q.all()

            # Series-type media (a show sitting in a list as a whole, #235):
            # the list cards read MediaTranslation on the Media row itself,
            # plus ShowTranslation on the linked Show row when one exists -
            # cover both. The episode-based show query above misses these
            # when the user never collected/watched an episode.
            series_q = await db.execute(
                select(Media.id, Media.tmdb_id)
                .where(Media.media_type == "series", Media.tmdb_id.isnot(None), has_items_filter)
                .distinct()
            )
            series_rows = series_q.all()

            series_show_q = await db.execute(
                select(ShowModel.id, ShowModel.tmdb_id)
                .join(Media, or_(Media.show_id == ShowModel.id, Media.tmdb_id == ShowModel.tmdb_id))
                .where(Media.media_type == "series", ShowModel.tmdb_id.isnot(None), has_items_filter)
                .distinct()
            )
            shows = list({tuple(r) for r in [*shows, *series_show_q.all()]})

        season_map: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for media_id, ep_num, season_num, show_tmdb_id in eps:
            season_map[(show_tmdb_id, season_num)].append((media_id, ep_num))

        total = len(movies) + len(shows) + len(season_map) + len(series_rows)
        state["total"] = total
        if total == 0:
            return

        # ── 2. Fetch all translations from TMDB in parallel ───────────────────
        sem = asyncio.Semaphore(5)

        # Collected results written to DB after all fetches complete
        movie_results: list[tuple[int, dict]] = []   # (media_id, data)
        show_results: list[tuple[int, dict]] = []    # (show_id, data)
        series_results: list[tuple[int, dict]] = []  # (media_id, show data)
        season_results: list[tuple[list[tuple[int, int]], dict]] = []  # (ep_list, season_data)

        async def fetch_movie(media_id: int, tmdb_id: int) -> None:
            async with sem:
                if not state.get("abort"):
                    try:
                        data = await tmdb_client.get_movie_light(tmdb_id, api_key=tmdb_key, language=language)
                        movie_results.append((media_id, data))
                    except Exception:
                        pass
            state["progress"] += 1

        async def fetch_show(show_id: int, show_tmdb_id: int) -> None:
            async with sem:
                if not state.get("abort"):
                    try:
                        data = await tmdb_client.get_show_light(show_tmdb_id, api_key=tmdb_key, language=language)
                        show_results.append((show_id, data))
                    except Exception:
                        pass
            state["progress"] += 1

        async def fetch_series_media(media_id: int, tmdb_id: int) -> None:
            async with sem:
                if not state.get("abort"):
                    try:
                        data = await tmdb_client.get_show_light(tmdb_id, api_key=tmdb_key, language=language)
                        series_results.append((media_id, data))
                    except Exception:
                        pass
            state["progress"] += 1

        async def fetch_season(show_tmdb_id: int, season_num: int, ep_list: list[tuple[int, int]]) -> None:
            async with sem:
                if not state.get("abort"):
                    try:
                        data = await tmdb_client.get_season(show_tmdb_id, season_num, api_key=tmdb_key, language=language)
                        season_results.append((ep_list, data))
                    except Exception:
                        pass
            state["progress"] += 1

        await asyncio.gather(
            *[fetch_movie(mid, tid) for mid, tid in movies],
            *[fetch_show(sid, stid) for sid, stid in shows],
            *[fetch_series_media(mid, tid) for mid, tid in series_rows],
            *[fetch_season(stid, snum, ep_list) for (stid, snum), ep_list in season_map.items()],
        )

        # ── 3. Write collected results to DB in one batch ─────────────────────
        async with AsyncSessionLocal() as db:
            for media_id, data in movie_results:
                await upsert_media_translation(
                    db, media_id, language,
                    data.get("title"), data.get("overview"),
                    data.get("tagline"), data.get("poster_path"),
                )
            for show_id, data in show_results:
                await upsert_show_translation(
                    db, show_id, language,
                    data.get("name"), data.get("overview"),
                    data.get("tagline"), data.get("poster_path"),
                )
            for media_id, data in series_results:
                await upsert_media_translation(
                    db, media_id, language,
                    data.get("name"), data.get("overview"),
                    data.get("tagline"), data.get("poster_path"),
                )
            for ep_list, season_data in season_results:
                ep_by_num = {ep["episode_number"]: ep for ep in (season_data.get("episodes") or [])}
                for media_id, ep_num in ep_list:
                    ep_data = ep_by_num.get(ep_num)
                    if ep_data:
                        await upsert_media_translation(
                            db, media_id, language,
                            ep_data.get("name"), ep_data.get("overview"),
                            None, ep_data.get("still_path"),
                        )
            await db.commit()

    except Exception as e:
        state["error"] = str(e)
    finally:
        state["running"] = False
        state["done"] = True


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


@router.get("/{user_id}", response_model=schemas.PublicProfileResponse)
async def get_public_profile(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_optional_user_or_api_key),
):
    user, profile = await _check_profile_access(user_id, current_user, db)
    is_owner = current_user and current_user.id == user_id
    is_admin = current_user and current_user.role == "admin"

    # --- Stats ---
    watched_q = await db.execute(
        select(func.count(func.distinct(WatchEvent.media_id)))
        .where(WatchEvent.user_id == user_id, WatchEvent.completed == True)
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
        .where(WatchEvent.user_id == user_id, WatchEvent.completed == True, Media.media_type == "movie")
    )
    movies_watched = movies_q.scalar_one()

    shows_q = await db.execute(
        select(func.count(func.distinct(ShowModel.id)))
        .join(Media, Media.show_id == ShowModel.id)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, WatchEvent.completed == True)
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
        .where(
            WatchEvent.user_id == user_id,
            WatchEvent.completed == True,
            Media.media_type == "movie",
            WatchEvent.watched_at.isnot(None),
        )
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
        .where(
            WatchEvent.user_id == user_id,
            WatchEvent.completed == True,
            Media.media_type == "episode",
            WatchEvent.watched_at.isnot(None),
        )
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
            "show_tvdb_id": show.tvdb_id if show else None,
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
    is_mutual_follow = False
    if current_user and current_user.id != user_id:
        follow_check = await db.execute(
            select(Follow).where(Follow.follower_id == current_user.id, Follow.following_id == user_id)
        )
        is_following = follow_check.scalar_one_or_none() is not None
        if is_following:
            follow_back = await db.execute(
                select(Follow).where(Follow.follower_id == user_id, Follow.following_id == current_user.id)
            )
            is_mutual_follow = follow_back.scalar_one_or_none() is not None

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
        # Mutual follows also get this user's friends-only lists (#210).
        visible = [PrivacyLevel.public]
        if is_mutual_follow:
            visible.append(PrivacyLevel.friends_only)
        lists_query = lists_query.where(ListModel.privacy_level.in_(visible))

    lists_result = await db.execute(lists_query)
    lists_rows = lists_result.all()
    list_ids = [row.id for row in lists_rows]

    # Fetch up to 3 preview posters per list using ROW_NUMBER
    posters_by_list: dict[int, list[dict]] = {}
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
            select(ListItem.list_id, poster_col, Media.adult, rn)
            .join(Media, ListItem.media_id == Media.id)
            .outerjoin(ShowAlias, ShowAlias.tmdb_id == Media.tmdb_id)
            .where(ListItem.list_id.in_(list_ids))
        ).subquery()
        posters_q = await db.execute(
            select(inner.c.list_id, inner.c.poster, inner.c.adult)
            .where(inner.c.rn <= 3)
            .where(inner.c.poster.isnot(None))
        )
        for row in posters_q.all():
            posters_by_list.setdefault(row.list_id, []).append({"url": row.poster, "adult": row.adult})

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


@router.get("/{user_id}/stats")
async def get_user_stats(
    user_id: int,
    since: Optional[DateType] = None,
    until: Optional[DateType] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_optional_user_or_api_key),
):
    await _check_profile_access(user_id, current_user, db)

    # Date range filters applied to all WatchEvent queries
    date_filters = [WatchEvent.completed == True]
    if since:
        date_filters.append(WatchEvent.watched_at >= since)
    if until:
        date_filters.append(WatchEvent.watched_at < until + TimeDelta(days=1))
    # Additional filter for anything bucketed/grouped by watched_at (activity charts,
    # weekday distribution, weeks-active count) — an unknown-dated event can't be
    # placed on a timeline, so it's excluded there while still counting in plain totals.
    dated_event_filters = [*date_filters, WatchEvent.watched_at.isnot(None)]

    # Activity granularity: daily for ranges ≤ 62 days, monthly otherwise
    if since and until:
        use_daily = (until - since).days <= 62
    else:
        use_daily = False
    activity_fmt = "YYYY-MM-DD" if use_daily else "YYYY-MM"

    # ── Watching ────────────────────────────────────────────────────────────
    movies_q = await db.execute(
        select(func.count(func.distinct(WatchEvent.media_id)))
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type == "movie", *date_filters)
    )
    movies_watched = movies_q.scalar_one()

    episodes_q = await db.execute(
        select(func.count(func.distinct(WatchEvent.media_id)))
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type == "episode", *date_filters)
    )
    episodes_watched = episodes_q.scalar_one()

    shows_q = await db.execute(
        select(func.count(func.distinct(ShowModel.id)))
        .join(Media, Media.show_id == ShowModel.id)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, *date_filters)
    )
    shows_watched = shows_q.scalar_one()

    from sqlalchemy import Integer as SAInteger
    from sqlalchemy.types import Text as SAText
    # Episodes store runtime in tmdb_data['runtime']; movies use Media.runtime.
    # JSON (non-JSONB) has no .astext; cast to Text first, guard against JSON "null".
    json_runtime = func.cast(
        func.nullif(func.cast(Media.tmdb_data["runtime"], SAText), "null"),
        SAInteger,
    )
    effective_runtime = func.coalesce(Media.runtime, json_runtime)
    watch_time_q = await db.execute(
        select(func.coalesce(func.sum(effective_runtime), 0))
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type.in_(["movie", "episode"]), *date_filters)
    )
    total_watch_minutes = watch_time_q.scalar_one() or 0

    movie_watch_time_q = await db.execute(
        select(func.coalesce(func.sum(effective_runtime), 0))
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type == "movie", *date_filters)
    )
    movie_watch_minutes = movie_watch_time_q.scalar_one() or 0

    show_watch_time_q = await db.execute(
        select(func.coalesce(func.sum(effective_runtime), 0))
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type == "episode", *date_filters)
    )
    show_watch_minutes = show_watch_time_q.scalar_one() or 0

    # Activity — GROUP BY must use the expression, not the label alias (PostgreSQL requirement)
    activity_expr = func.to_char(WatchEvent.watched_at, activity_fmt)
    activity_q = await db.execute(
        select(
            activity_expr.label("period"),
            Media.media_type,
            func.count(func.distinct(WatchEvent.media_id)).label("cnt"),
        )
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type.in_(["movie", "episode"]), *dated_event_filters)
        .group_by(activity_expr, Media.media_type)
        .order_by(activity_expr)
    )
    activity_rows = activity_q.all()
    activity_map: dict[str, dict] = {}
    for period_key, mtype, cnt in activity_rows:
        if period_key not in activity_map:
            activity_map[period_key] = {"month": period_key, "movies": 0, "episodes": 0}
        if mtype == "movie":
            activity_map[period_key]["movies"] = cnt
        else:
            activity_map[period_key]["episodes"] = cnt
    watch_activity = sorted(activity_map.values(), key=lambda x: x["month"])

    # Watch time per period (same granularity as activity)
    watch_time_activity_q = await db.execute(
        select(
            activity_expr.label("period"),
            Media.media_type,
            func.coalesce(func.sum(effective_runtime), 0).label("minutes"),
        )
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type.in_(["movie", "episode"]), *dated_event_filters)
        .group_by(activity_expr, Media.media_type)
        .order_by(activity_expr)
    )
    watch_time_map: dict[str, dict] = {}
    for period_key, mtype, minutes in watch_time_activity_q.all():
        if period_key not in watch_time_map:
            watch_time_map[period_key] = {"month": period_key, "movie_minutes": 0, "show_minutes": 0}
        if mtype == "movie":
            watch_time_map[period_key]["movie_minutes"] = int(minutes)
        else:
            watch_time_map[period_key]["show_minutes"] = int(minutes)
    watch_time_activity = sorted(watch_time_map.values(), key=lambda x: x["month"])

    # Average watches per weekday (0=Sun … 6=Sat)
    from sqlalchemy import extract
    dow_expr = func.extract("dow", WatchEvent.watched_at)
    dow_q = await db.execute(
        select(
            dow_expr.label("dow"),
            func.count(func.distinct(WatchEvent.media_id)).label("cnt"),
        )
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type.in_(["movie", "episode"]), *dated_event_filters)
        .group_by(dow_expr)
        .order_by(dow_expr)
    )
    dow_raw = {int(row.dow): row.cnt for row in dow_q.all()}

    # Count distinct weeks to normalise
    weeks_q = await db.execute(
        select(func.count(func.distinct(func.to_char(WatchEvent.watched_at, "IYYY-IW"))))
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, Media.media_type.in_(["movie", "episode"]), *dated_event_filters)
    )
    total_weeks = max(weeks_q.scalar_one() or 1, 1)
    day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    weekday_activity = [
        {"day": day_names[i], "avg": round(dow_raw.get(i, 0) / total_weeks, 2)}
        for i in range(7)
    ]

    # ── Genre Activity ───────────────────────────────────────────────────────
    # Movies Genres
    movie_genres_q = await db.execute(
        select(Media.tmdb_data["genres"])
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == user_id,
            Media.media_type == "movie",
            Media.tmdb_data["genres"].isnot(None),
            *date_filters
        )
    )
    movie_genre_counts = {}
    for (genres_list,) in movie_genres_q.all():
        if genres_list:
            for g in genres_list:
                name = g["name"] if isinstance(g, dict) else g
                movie_genre_counts[name] = movie_genre_counts.get(name, 0) + 1

    # Shows Genres
    show_genres_q = await db.execute(
        select(ShowModel.tmdb_data["genres"])
        .join(Media, Media.show_id == ShowModel.id)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == user_id,
            Media.media_type == "episode",
            ShowModel.tmdb_data["genres"].isnot(None),
            *date_filters
        )
    )
    show_genre_counts = {}
    for (genres_list,) in show_genres_q.all():
        if genres_list:
            for g in genres_list:
                name = g["name"] if isinstance(g, dict) else g
                show_genre_counts[name] = show_genre_counts.get(name, 0) + 1

    top_movie_genres = sorted(
        [{"name": k, "count": v} for k, v in movie_genre_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    top_show_genres = sorted(
        [{"name": k, "count": v} for k, v in show_genre_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    # ── Rating date filters mirror the watch event date filters but on rated_at
    rating_date_filters = []
    if since:
        rating_date_filters.append(Rating.rated_at >= since)
    if until:
        rating_date_filters.append(Rating.rated_at < until + TimeDelta(days=1))

    rating_dist_q = await db.execute(
        select(Rating.rating, func.count(Rating.id).label("cnt"))
        .where(Rating.user_id == user_id, Rating.rating.isnot(None), *rating_date_filters)
        .group_by(Rating.rating)
        .order_by(Rating.rating)
    )
    rating_distribution = [
        {"rating": float(r), "count": c} for r, c in rating_dist_q.all()
    ]

    avg_movie_rating_q = await db.execute(
        select(func.avg(Rating.rating))
        .join(Media, Rating.media_id == Media.id)
        .where(Rating.user_id == user_id, Rating.rating.isnot(None), Media.media_type == "movie", *rating_date_filters)
    )
    avg_movie_rating = avg_movie_rating_q.scalar_one()
    avg_movie_rating = round(float(avg_movie_rating), 2) if avg_movie_rating else None

    avg_show_rating_q = await db.execute(
        select(func.avg(Rating.rating))
        .join(Media, Rating.media_id == Media.id)
        .where(Rating.user_id == user_id, Rating.rating.isnot(None), Media.media_type == "series", *rating_date_filters)
    )
    avg_show_rating = avg_show_rating_q.scalar_one()
    avg_show_rating = round(float(avg_show_rating), 2) if avg_show_rating else None

    # ── Collection ───────────────────────────────────────────────────────────
    movies_collected_q = await db.execute(
        select(func.count(func.distinct(Collection.media_id)))
        .join(Media, Collection.media_id == Media.id)
        .where(Collection.user_id == user_id, Media.media_type == "movie")
    )
    movies_collected = movies_collected_q.scalar_one()

    episodes_collected_q = await db.execute(
        select(func.count(func.distinct(Collection.media_id)))
        .join(Media, Collection.media_id == Media.id)
        .where(Collection.user_id == user_id, Media.media_type == "episode")
    )
    episodes_collected = episodes_collected_q.scalar_one()

    shows_collected_q = await db.execute(
        select(func.count(func.distinct(ShowModel.id)))
        .join(Media, Media.show_id == ShowModel.id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == user_id)
    )
    shows_collected = shows_collected_q.scalar_one()

    # Unwatched movies in collection
    watched_movie_ids_sub = (
        select(WatchEvent.media_id)
        .join(Media, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id, WatchEvent.completed == True, Media.media_type == "movie")
        .scalar_subquery()
    )
    unwatched_movies_q = await db.execute(
        select(func.count(func.distinct(Collection.media_id)))
        .join(Media, Collection.media_id == Media.id)
        .where(
            Collection.user_id == user_id,
            Media.media_type == "movie",
            Collection.media_id.notin_(watched_movie_ids_sub),
        )
    )
    unwatched_movies = unwatched_movies_q.scalar_one()

    # Shows watched from collection (has at least one watched episode in collection)
    watched_shows_collected_q = await db.execute(
        select(func.count(func.distinct(ShowModel.id)))
        .join(Media, Media.show_id == ShowModel.id)
        .join(Collection, Collection.media_id == Media.id)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(Collection.user_id == user_id, WatchEvent.user_id == user_id, WatchEvent.completed == True)
    )
    shows_watched_collected = watched_shows_collected_q.scalar_one()

    # ── Top titles, networks and people (#124) ──────────────────────────────
    # Own-profile views kick the TMDB credits backfill (background, throttled
    # by a TTL in core/credits.py); the people/studio groups on the page stay
    # hidden until credits data exists.
    if current_user and current_user.id == user_id:
        await maybe_backfill_credits(db, user_id)

    top_rows = (await db.execute(
        select(
            Media.media_type, Media.id, Media.title, Media.poster_path,
            Media.tmdb_id, Media.show_id, effective_runtime.label("runtime"),
        )
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(
            WatchEvent.user_id == user_id,
            Media.media_type.in_(["movie", "episode"]),
            *date_filters,
        )
    )).all()

    per_show: dict[int, dict] = defaultdict(lambda: {"plays": 0, "minutes": 0, "episodes": set()})
    per_movie: dict[int, dict] = {}
    for media_type, mid, m_title, m_poster, m_tmdb_id, show_id, runtime in top_rows:
        minutes = runtime or 0
        if media_type == "episode" and show_id:
            s = per_show[show_id]
            s["plays"] += 1
            s["minutes"] += minutes
            s["episodes"].add(mid)
        elif media_type == "movie":
            m = per_movie.setdefault(mid, {
                "title": m_title, "poster_path": m_poster, "tmdb_id": m_tmdb_id,
                "plays": 0, "minutes": 0,
            })
            m["plays"] += 1
            m["minutes"] += minutes

    show_meta: dict[int, dict] = {}
    if per_show:
        top_show_rows = (await db.execute(
            select(ShowModel).where(ShowModel.id.in_(per_show.keys()))
        )).scalars().all()
        for sh in top_show_rows:
            show_meta[sh.id] = {
                "title": sh.title, "poster_path": sh.poster_path,
                "tmdb_id": sh.tmdb_id, "tvdb_id": sh.tvdb_id,
            }

    top_shows = sorted(
        ({**show_meta.get(sid, {"title": None, "poster_path": None, "tmdb_id": None, "tvdb_id": None}),
          "plays": s["plays"], "minutes": s["minutes"], "episodes": len(s["episodes"])}
         for sid, s in per_show.items()),
        key=lambda x: (x["plays"], x["minutes"]), reverse=True,
    )[:12]
    top_movies = sorted(per_movie.values(), key=lambda x: (x["plays"], x["minutes"]), reverse=True)[:12]
    # Networks come from the same instance-wide TMDB credits cache as the people
    # stats below (core/credits.py) - it already fetches a full show response per
    # watched title, so this reuses that instead of relying on Show.tmdb_data
    # (which detail pages fetch live and never persist networks into anyway).
    top_people = await credits_stats(db, user_id, date_filters)
    top_networks = top_people.pop("networks")

    return {
        # Watching
        "granularity": "day" if use_daily else "month",
        "weekday_activity": weekday_activity,
        "movies_watched": movies_watched,
        "shows_watched": shows_watched,
        "episodes_watched": episodes_watched,
        "total_watch_minutes": total_watch_minutes,
        "movie_watch_minutes": movie_watch_minutes,
        "show_watch_minutes": show_watch_minutes,
        "watch_activity": watch_activity,
        "watch_time_activity": watch_time_activity,
        "rating_distribution": rating_distribution,
        "avg_movie_rating": avg_movie_rating,
        "avg_show_rating": avg_show_rating,
        "top_movie_genres": top_movie_genres,
        "top_show_genres": top_show_genres,
        # Collection
        "movies_collected": movies_collected,
        "shows_collected": shows_collected,
        "episodes_collected": episodes_collected,
        "movies_watched_collected": movies_collected - unwatched_movies,
        "movies_unwatched_collected": unwatched_movies,
        "shows_watched_collected": shows_watched_collected,
        "shows_unwatched_collected": max(shows_collected - shows_watched_collected, 0),
        # Top titles & people (#124)
        "top_shows": top_shows,
        "top_movies": top_movies,
        "top_networks": top_networks,
        "top_people": top_people,
    }
