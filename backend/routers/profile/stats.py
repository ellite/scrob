from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from datetime import date as DateType, timedelta as TimeDelta
from typing import Optional
from db import get_db
from core.credits import credits_stats, maybe_backfill_credits
from core.watch_stats import effective_runtime_expression
from dependencies import get_optional_user_or_api_key
from models.users import User
from models.events import WatchEvent
from models.collection import Collection
from models.media import Media
from models.ratings import Rating
from models.show import Show as ShowModel

from .common import _check_profile_access
from .top_media import _top_media, _watch_date_filters


router = APIRouter()


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
    date_filters = _watch_date_filters(since, until)
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

    effective_runtime = effective_runtime_expression()
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

    top_shows, top_movies = await _top_media(db, user_id, date_filters)
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
        "top_shows": top_shows[:12],
        "top_movies": top_movies[:12],
        "top_networks": top_networks,
        "top_people": top_people,
    }
