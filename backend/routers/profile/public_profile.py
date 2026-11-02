from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, case
from sqlalchemy.orm import aliased
from db import get_db
from dependencies import get_optional_user_or_api_key
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
import schemas

from .common import _apply_viewer_language, _check_profile_access, _latest_episode_per_show_subquery


router = APIRouter()


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
    rw_movies_rows = rw_movies_q.all()
    recently_watched_movies = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "movie",
            "title": media.title,
            "poster_path": media.poster_path,
            "watched_at": we.watched_at.isoformat(),
        }
        for we, media in rw_movies_rows
    ]
    await _apply_viewer_language(db, current_user, [(i, m, None) for i, (_w, m) in zip(recently_watched_movies, rw_movies_rows)])

    # --- Recently Watched Shows (episodes) ---
    # One card per show, not per episode - a binge session shouldn't fill the
    # whole rail with the same show.
    latest_episode_per_show = _latest_episode_per_show_subquery(user_id)

    rw_shows_q = await db.execute(
        select(WatchEvent, Media, ShowModel)
        .join(latest_episode_per_show, WatchEvent.id == latest_episode_per_show.c.we_id)
        .join(Media, WatchEvent.media_id == Media.id)
        .outerjoin(ShowModel, Media.show_id == ShowModel.id)
        .order_by(WatchEvent.watched_at.desc())
        .limit(12)
    )
    rw_shows_rows = rw_shows_q.all()
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
        for we, media, show in rw_shows_rows
    ]
    await _apply_viewer_language(db, current_user, [(i, m, sh) for i, (_w, m, sh) in zip(recently_watched_shows, rw_shows_rows)])

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
    tr_movies_rows = tr_movies_q.all()
    top_rated_movies = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "movie",
            "title": media.title,
            "poster_path": media.poster_path,
            "user_rating": rating.rating,
        }
        for rating, media in tr_movies_rows
    ]
    await _apply_viewer_language(db, current_user, [(i, m, None) for i, (_r, m) in zip(top_rated_movies, tr_movies_rows)])

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
    tr_shows_rows = tr_shows_q.all()
    top_rated_shows = [
        {
            "tmdb_id": media.tmdb_id,
            "media_type": "series",
            "title": media.title,
            "poster_path": show.poster_path if show else media.poster_path,
            "user_rating": rating.rating,
        }
        for rating, media, show in tr_shows_rows
    ]
    await _apply_viewer_language(db, current_user, [(i, m, sh) for i, (_r, m, sh) in zip(top_rated_shows, tr_shows_rows)])

    # --- Recent Comments ---
    recent_comments_q = await db.execute(
        select(CommentModel)
        .where(CommentModel.user_id == user_id)
        .order_by(CommentModel.created_at.desc())
        .limit(5)
    )
    comments_list = recent_comments_q.scalars().all()

    # Batch resolve titles for comments
    show_tmdb_ids = list({c.tmdb_id for c in comments_list if c.media_type in ("series", "season", "episode") and c.tmdb_id})
    show_tvdb_ids = list({c.tvdb_id for c in comments_list if c.media_type in ("series", "season", "episode") and c.tvdb_id})
    movie_tmdb_ids = list({c.tmdb_id for c in comments_list if c.media_type == "movie" and c.tmdb_id})

    show_titles: dict[int, tuple[str, str | None]] = {}
    movie_titles: dict[int, tuple[str, str | None]] = {}

    if show_tmdb_ids:
        sq = await db.execute(
            select(ShowModel.tmdb_id, ShowModel.title, ShowModel.poster_path)
            .where(ShowModel.tmdb_id.in_(show_tmdb_ids))
        )
        for tmdb_id, title, poster_path in sq.all():
            show_titles[tmdb_id] = (title, poster_path)

    show_tvdb_titles: dict[int, tuple[str, str | None]] = {}
    if show_tvdb_ids:
        tq = await db.execute(
            select(ShowModel.tvdb_id, ShowModel.title, ShowModel.poster_path)
            .where(ShowModel.tvdb_id.in_(show_tvdb_ids))
        )
        for tvdb_id, title, poster_path in tq.all():
            show_tvdb_titles[tvdb_id] = (title, poster_path)

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
            info = show_titles.get(c.tmdb_id) if c.tmdb_id else show_tvdb_titles.get(c.tvdb_id)
        else:
            info = movie_titles.get(c.tmdb_id)
        recent_comments.append({
            "id": c.id,
            "content": c.content,
            "media_type": c.media_type,
            "tmdb_id": c.tmdb_id,
            "tvdb_id": c.tvdb_id,
            "season_number": c.season_number,
            "episode_number": c.episode_number,
            "title": info[0] if info else None,
            "poster_path": info[1] if info else None,
            "created_at": c.created_at.isoformat(),
            "wetrakr_comment_id": c.wetrakr_comment_id,
            "wetrakr_source": (c.wetrakr_source or "wetrakr") if c.wetrakr_comment_id else None,
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
