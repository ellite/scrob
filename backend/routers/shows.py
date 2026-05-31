import asyncio
from datetime import datetime, date
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, cast as sa_cast, Text, or_, and_
from sqlalchemy.orm import aliased

from models.events import WatchEvent
from models.collection import Collection, CollectionFile
from models.ratings import Rating
from models.lists import List as UserList, ListItem

from db import get_db
from models.media import Media
from models.collection import Collection, CollectionFile
from models.base import MediaType
from models.show import Show as ShowModel
from models.users import User, UserSettings
from routers.media import format_media, get_user_tmdb_key, check_tmdb_key, enrich_with_state, refresh_technical_data, _extract_show_content_rating, get_where_to_watch, _effective_sonarr, _get_global_settings

from dependencies import get_current_user
from core import tmdb
from core import tvdb as tvdb_client

router = APIRouter()


async def get_user_tvdb_key(db: AsyncSession, user_id: int) -> str | None:
    from models.global_settings import GlobalSettings
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    s = result.scalar_one_or_none()
    if s and s.tvdb_api_key:
        return s.tvdb_api_key
    gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = gs_result.scalar_one_or_none()
    return gs.tvdb_api_key if gs else None


def format_show(show: ShowModel) -> dict:
    return {
        "id": show.id,
        "tmdb_id": show.tmdb_id,
        "tvdb_id": show.tvdb_id,
        "type": "series",
        "title": show.title,
        "original_title": show.original_title,
        "overview": show.overview,
        "poster_path": show.poster_path,
        "backdrop_path": show.backdrop_path,
        "tmdb_rating": show.tmdb_rating,
        "status": show.status,
        "tagline": show.tagline,
        "first_air_date": show.first_air_date,
        "last_air_date": show.last_air_date,
        "genres": [g["name"] if isinstance(g, dict) else g for g in (show.tmdb_data or {}).get("genres", [])],
        "seasons_meta": (show.tmdb_data or {}).get("seasons", []),
        "original_language": (show.tmdb_data or {}).get("original_language"),
        "adult": (show.tmdb_data or {}).get("adult", False),
    }


@router.get("")
async def list_shows(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    sort: str = Query(default="title"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    genre: str | None = Query(None),
    year: int | None = Query(None),
    status: str | None = Query(None),
):
    offset = (page - 1) * page_size

    # A show is "in the user's collection" if they have at least one countable episode
    # collected — same criteria used by enrich_with_state for the percentage calculation.
    user_show_ids = (
        select(Media.show_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.show_id.isnot(None),
            Media.media_type == MediaType.episode,
            Media.season_number.isnot(None),
            Media.season_number != 0,
            Media.episode_number.isnot(None),
        )
        .distinct()
        .subquery()
    )

    base_query = select(ShowModel).where(ShowModel.id.in_(select(user_show_ids)))
    if genre:
        base_query = base_query.where(sa_cast(ShowModel.tmdb_data["genres"], Text).contains(f'"{genre}"'))
    if year:
        base_query = base_query.where(ShowModel.first_air_date.like(f'{year}%'))
    if status:
        base_query = base_query.where(ShowModel.status == status)

    # Count total
    count_query = select(func.count()).select_from(base_query.subquery())
    total_result = await db.execute(count_query)
    total_count = total_result.scalar_one()
    total_pages = (total_count + page_size - 1) // page_size

    # Sort and Paginate
    if sort == "rating":
        q = base_query.order_by(ShowModel.tmdb_rating.desc().nulls_last())
    elif sort == "release_date":
        q = base_query.order_by(ShowModel.first_air_date.desc().nulls_last())
    elif sort == "created_at":
        q = base_query.order_by(ShowModel.created_at.desc().nulls_last())
    elif sort == "last_watched":
        last_watched_sq = (
            select(Media.show_id, func.max(WatchEvent.watched_at).label("last_watched_at"))
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(WatchEvent.user_id == current_user.id, Media.show_id.isnot(None))
            .group_by(Media.show_id)
            .subquery()
        )
        q = (
            base_query
            .outerjoin(last_watched_sq, last_watched_sq.c.show_id == ShowModel.id)
            .order_by(last_watched_sq.c.last_watched_at.desc().nulls_last())
        )
    else:
        q = base_query.order_by(func.lower(ShowModel.title).asc())

    result = await db.execute(q.limit(page_size).offset(offset))
    results = [format_show(s) for s in result.scalars().all()]
    await enrich_with_state(db, current_user.id, results)
    return {
        "page": page,
        "page_size": page_size,
        "total_results": total_count,
        "total_pages": total_pages,
        "results": results,
    }


@router.get("/{series_tmdb_id}")
async def get_show(
    series_tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. Try to find locally
    show_result = await db.execute(
        select(ShowModel).where(ShowModel.tmdb_id == series_tmdb_id)
    )
    show = show_result.scalar_one_or_none()

    if show:
        # Fetch local episodes
        episodes_result = await db.execute(
            select(Media)
            .where(Media.media_type == MediaType.episode)
            .where(Media.show_id == show.id)
            .order_by(Media.season_number.asc(), Media.episode_number.asc())
        )
        episodes = episodes_result.scalars().all()

        seasons_meta = {
            s["season_number"]: s for s in (show.tmdb_data or {}).get("seasons", [])
        }

        seasons: dict = {}
        for ep in episodes:
            s_num = ep.season_number or 0
            season_poster = (
                seasons_meta.get(s_num, {}).get("poster_path") or show.poster_path
            )
            ep_formatted = format_media(ep)
            ep_formatted["poster_path"] = ep.poster_path or season_poster
            seasons.setdefault(s_num, []).append(ep_formatted)

        # Fetch networks + recommendations from TMDB if key is available
        networks = []
        recommendations = []
        cast = []
        tmdb_extra: dict | None = None
        api_key = await get_user_tmdb_key(db, current_user.id)
        if check_tmdb_key(api_key):
            try:
                tmdb_extra = await tmdb.get_show(series_tmdb_id, api_key=api_key)
                networks = [
                    {
                        "id": n["id"],
                        "name": n["name"],
                        "logo_path": tmdb.poster_url(n.get("logo_path"), size="w500")
                        if n.get("logo_path")
                        else None,
                        "origin_country": n.get("origin_country"),
                    }
                    for n in tmdb_extra.get("networks", [])
                ]
                recommendations = [
                    {
                        "id": None,
                        "tmdb_id": r["id"],
                        "type": "series",
                        "title": r.get("name") or r.get("title"),
                        "original_title": r.get("original_name")
                        or r.get("original_title"),
                        "overview": r.get("overview"),
                        "poster_path": tmdb.poster_url(r.get("poster_path")),
                        "backdrop_path": tmdb.poster_url(
                            r.get("backdrop_path"), size="w1280"
                        ),
                        "release_date": r.get("first_air_date")
                        or r.get("release_date"),
                        "tmdb_rating": r.get("vote_average"),
                        "adult": r.get("adult", False),
                    }
                    for r in tmdb_extra.get("recommendations", {}).get("results", [])[
                        :12
                    ]
                ]
                await enrich_with_state(db, current_user.id, recommendations)
                cast = [
                    {
                        "tmdb_id": c.get("id"),
                        "name": c.get("name"),
                        "character": c.get("character"),
                        "profile_path": tmdb.poster_url(c.get("profile_path")),
                    }
                    for c in tmdb_extra.get("credits", {}).get("cast", [])[:12]
                ]
            except Exception:
                pass

        state_item: dict = {"tmdb_id": series_tmdb_id, "type": "series"}
        # Pass last_episode_to_air from the already-fetched tmdb_extra so enrich_with_state
        # doesn't make a redundant second TMDB call for the same data.
        if tmdb_extra and tmdb_extra.get("last_episode_to_air"):
            state_item["_last_episode_to_air"] = tmdb_extra["last_episode_to_air"]
        await enrich_with_state(db, current_user.id, [state_item])

        # --- Per-season states ---
        season_states: dict = {}

        # Collected and watched episode counts per season in one query
        coll_a  = aliased(Collection)
        watch_a = aliased(WatchEvent)
        season_stats_q = await db.execute(
            select(
                Media.season_number,
                func.count(func.distinct(
                    case((coll_a.id.isnot(None), Media.episode_number), else_=None)
                )).label("collected"),
                func.count(func.distinct(
                    case((watch_a.id.isnot(None), Media.episode_number), else_=None)
                )).label("watched"),
            )
            .outerjoin(coll_a, and_(coll_a.media_id == Media.id, coll_a.user_id == current_user.id))
            .outerjoin(watch_a, and_(watch_a.media_id == Media.id, watch_a.user_id == current_user.id))
            .where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.episode_number.isnot(None),
            )
            .group_by(Media.season_number)
        )
        coll_per_season: dict = {}
        watched_per_season: dict = {}
        for sn, collected, watched in season_stats_q.all():
            coll_per_season[sn] = collected
            watched_per_season[sn] = watched

        # Season user ratings (stored against the show's Media row with season_number)
        show_media_q = await db.execute(
            select(Media).where(Media.tmdb_id == series_tmdb_id, Media.media_type == MediaType.series)
        )
        show_media = show_media_q.scalar_one_or_none()
        if show_media and not show_media.adult and (tmdb_extra or {}).get("adult", False):
            show_media.adult = True
            await db.commit()
        season_ratings: dict = {}
        if show_media:
            ratings_q = await db.execute(
                select(Rating.season_number, func.max(Rating.rating))
                .where(
                    Rating.media_id == show_media.id,
                    Rating.user_id == current_user.id,
                    Rating.season_number.isnot(None),
                )
                .group_by(Rating.season_number)
            )
            season_ratings = dict(ratings_q.all())

        # Episode counts per season from stored TMDB metadata
        season_ep_counts: dict = {
            s["season_number"]: s.get("episode_count", 0)
            for s in (show.tmdb_data or {}).get("seasons", [])
        }

        # For shows that are still airing, adjust the counts to only include aired episodes
        last_ep = (tmdb_extra or show.tmdb_data or {}).get("last_episode_to_air")
        if last_ep:
            last_sn = last_ep.get("season_number")
            last_en = last_ep.get("episode_number")
            if last_sn in season_ep_counts:
                # Capped at the last aired episode number for the current season
                season_ep_counts[last_sn] = last_en
            # Future seasons have 0 aired episodes
            for sn in season_ep_counts:
                if sn > last_sn:
                    season_ep_counts[sn] = 0

        # Build season states for all known seasons
        for sn in set(list(coll_per_season.keys()) + list(season_ep_counts.keys())):
            collected = coll_per_season.get(sn, 0)
            watched = watched_per_season.get(sn, 0)
            total = season_ep_counts.get(sn, 0)
            
            # Use distinct (season, episode) from user's collection for calculation
            # to be consistent with how total is calculated (unique episodes in season).
            season_states[sn] = {
                "in_library": collected > 0,
                "collection_pct": min(100, int((collected / total) * 100)) if total > 0 else 0,
                "watched": watched >= total if total > 0 else False,
                "watch_pct": min(100, int((watched / total) * 100)) if total > 0 else 0,
                "user_rating": season_ratings.get(sn),
            }

        # Enhance seasons_meta with live TMDB data (id, rating, overview, air_date)
        tmdb_season_map: dict = {}
        if tmdb_extra:
            tmdb_season_map = {s["season_number"]: s for s in tmdb_extra.get("seasons", [])}
        base_seasons_meta = (show.tmdb_data or {}).get("seasons", [])
        enhanced_seasons_meta = [
            {
                **s,
                "tmdb_season_id": tmdb_season_map.get(s["season_number"], {}).get("id"),
                "tmdb_rating": tmdb_season_map.get(s["season_number"], {}).get("vote_average"),
                # Fill overview/air_date from live TMDB if not stored in DB
                "overview": s.get("overview") or tmdb_season_map.get(s["season_number"], {}).get("overview"),
                "air_date": s.get("air_date") or tmdb_season_map.get(s["season_number"], {}).get("air_date"),
            }
            for s in base_seasons_meta
        ]

        where_to_watch = await get_where_to_watch(
            db, current_user.id, series_tmdb_id, MediaType.series, show=show, tmdb_key=api_key
        )

        return {
            **format_show(show),
            "seasons_meta": enhanced_seasons_meta,
            "original_language": (show.tmdb_data or {}).get("original_language") or (tmdb_extra or {}).get("original_language"),
            "age_rating": _extract_show_content_rating(tmdb_extra) if tmdb_extra else None,
            "imdb_id": (tmdb_extra or show.tmdb_data or {}).get("external_ids", {}).get("imdb_id"),
            "adult": (tmdb_extra or show.tmdb_data or {}).get("adult", False),
            "in_library": state_item.get("collection_pct", 0) > 0 if state_item else False,
            "watched": state_item.get("watched", False) if state_item else False,
            "in_lists": state_item.get("in_lists", []),
            "collection_pct": state_item.get("collection_pct", 0),
            "watch_pct": state_item.get("watch_pct", 0),
            "is_monitored": state_item.get("is_monitored", False),
            "request_enabled": state_item.get("request_enabled", False),
            "request_status": state_item.get("request_status"),
            "user_rating": state_item.get("user_rating"),
            "season_states": season_states,
            "seasons": {f"season_{k}": v for k, v in sorted(seasons.items())},
            "created_by": [
                {"tmdb_id": c["id"], "name": c["name"]}
                for c in (tmdb_extra or show.tmdb_data or {}).get("created_by", [])
            ],
            "cast": cast,
            "networks": networks,
            "where_to_watch": where_to_watch,
        }

    # 2. If not local, fetch from TMDB
    api_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(api_key):
        raise HTTPException(
            status_code=404, detail="Show not found and TMDB key not configured"
        )

    try:
        data = await tmdb.get_show(series_tmdb_id, api_key=api_key)

        cast = [
            {
                "tmdb_id": c.get("id"),
                "name": c.get("name"),
                "character": c.get("character"),
                "profile_path": tmdb.poster_url(c.get("profile_path")),
            }
            for c in data.get("credits", {}).get("cast", [])[:12]
        ]

        networks = [
            {
                "id": n["id"],
                "name": n["name"],
                "logo_path": tmdb.poster_url(n.get("logo_path"), size="w500")
                if n.get("logo_path")
                else None,
                "origin_country": n.get("origin_country"),
            }
            for n in data.get("networks", [])
        ]

        state_item_tmdb: dict = {"tmdb_id": series_tmdb_id, "type": "series"}
        await enrich_with_state(db, current_user.id, [state_item_tmdb])

        where_to_watch = await get_where_to_watch(
            db, current_user.id, series_tmdb_id, MediaType.series, tmdb_key=api_key
        )

        return {
            "id": None,
            "tmdb_id": series_tmdb_id,
            "title": data.get("name"),
            "original_title": data.get("original_name"),
            "overview": data.get("overview"),
            "poster_path": tmdb.poster_url(data.get("poster_path")),
            "backdrop_path": tmdb.poster_url(data.get("backdrop_path"), size="w1280"),
            "tmdb_rating": data.get("vote_average"),
            "status": data.get("status"),
            "tagline": data.get("tagline"),
            "first_air_date": data.get("first_air_date"),
            "last_air_date": data.get("last_air_date"),
            "genres": [g["name"] for g in data.get("genres", [])],
            "original_language": data.get("original_language"),
            "age_rating": _extract_show_content_rating(data),
            "imdb_id": data.get("external_ids", {}).get("imdb_id"),
            "adult": data.get("adult", False),
            "in_library": state_item_tmdb.get("collection_pct", 0) > 0,
            "watched": state_item_tmdb.get("watched", False),
            "in_lists": state_item_tmdb.get("in_lists", []),
            "collection_pct": state_item_tmdb.get("collection_pct", 0),
            "is_monitored": state_item_tmdb.get("is_monitored", False),
            "request_enabled": state_item_tmdb.get("request_enabled", False),
            "request_status": state_item_tmdb.get("request_status"),
            "user_rating": state_item_tmdb.get("user_rating"),
            "created_by": [
                {"tmdb_id": c["id"], "name": c["name"]}
                for c in data.get("created_by", [])
            ],
            "cast": cast,
            "networks": networks,
            "seasons_meta": [
                {
                    "season_number": s["season_number"],
                    "poster_path": tmdb.poster_url(s.get("poster_path")),
                    "episode_count": s["episode_count"],
                    "name": s["name"],
                    "air_date": s.get("air_date"),
                    "overview": s.get("overview"),
                    "tmdb_rating": s.get("vote_average"),
                }
                for s in data.get("seasons", [])
            ],
            "seasons": {},
            "season_states": {},
            "where_to_watch": where_to_watch,
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"TMDB Show not found: {e}")


@router.get("/{series_tmdb_id}/recommendations")
async def get_show_recommendations(
    series_tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch series recommendations from TMDB and enrich with state."""
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}

    try:
        data = await tmdb.get_show(series_tmdb_id, api_key=tmdb_key)
        recs_raw = data.get("recommendations", {}).get("results", [])[:12]
        recommendations = [
            {
                "id": None,
                "tmdb_id": r["id"],
                "type": "series",
                "title": r.get("name") or r.get("title"),
                "original_title": r.get("original_name") or r.get("original_title"),
                "overview": r.get("overview"),
                "poster_path": tmdb.poster_url(r.get("poster_path")),
                "backdrop_path": tmdb.poster_url(r.get("backdrop_path"), size="w1280"),
                "release_date": r.get("first_air_date") or r.get("release_date"),
                "tmdb_rating": r.get("vote_average"),
            }
            for r in recs_raw
        ]
        await enrich_with_state(db, current_user.id, recommendations)
        return {"results": recommendations}
    except Exception:
        return {"results": []}


@router.get("/{series_tmdb_id}/season/{season_number}")
async def get_show_season(
    series_tmdb_id: int,
    season_number: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. Try to find show and local episodes for this season
    show_result = await db.execute(
        select(ShowModel).where(ShowModel.tmdb_id == series_tmdb_id)
    )
    show = show_result.scalar_one_or_none()

    local_episodes = []
    if show:
        ep_result = await db.execute(
            select(Media)
            .where(Media.media_type == MediaType.episode)
            .where(Media.show_id == show.id)
            .where(Media.season_number == season_number)
            .order_by(Media.episode_number.asc())
        )
        local_episodes = ep_result.scalars().all()

    # 2. Always fetch full season data from TMDB for consistent metadata
    api_key = await get_user_tmdb_key(db, current_user.id)

    try:
        if check_tmdb_key(api_key):
            import asyncio

            # Fetch season and show info (if not local) in parallel
            if not show:
                tmdb_data, tmdb_show_data = await asyncio.gather(
                    tmdb.get_season(series_tmdb_id, season_number, api_key=api_key),
                    tmdb.get_show(series_tmdb_id, api_key=api_key),
                )
                show_info = {
                    "id": None,
                    "tmdb_id": series_tmdb_id,
                    "title": tmdb_show_data.get("name"),
                    "poster_path": tmdb.poster_url(tmdb_show_data.get("poster_path")),
                    "backdrop_path": tmdb.poster_url(
                        tmdb_show_data.get("backdrop_path"), size="w1280"
                    ),
                }
            else:
                tmdb_data = await tmdb.get_season(
                    series_tmdb_id, season_number, api_key=api_key
                )
                show_info = format_show(show)

            # Bulk fetch watched state and ratings for episodes in this season
            tmdb_episodes = tmdb_data.get("episodes", [])
            total_in_season = len(tmdb_episodes)
            today_str = date.today().isoformat()
            total_aired_in_season = sum(
                1 for ep in tmdb_episodes
                if ep.get("air_date") and ep["air_date"] <= today_str
            )
            season_ep_tmdb_ids = [
                ep.get("id") for ep in tmdb_episodes if ep.get("id")
            ]

            watched_ep_ids: set = set()
            episode_ratings: dict = {}

            # Find all local Media rows for these episodes (even if show_id is null)
            # and map them by TMDB ID for easy lookup.
            local_media_by_tmdb: dict[int, Media] = {}
            if season_ep_tmdb_ids:
                media_q = await db.execute(
                    select(Media).where(
                        Media.media_type == MediaType.episode,
                        Media.tmdb_id.in_(season_ep_tmdb_ids)
                    )
                )
                for m in media_q.scalars().all():
                    local_media_by_tmdb[m.tmdb_id] = m

            local_media_ids = [m.id for m in local_media_by_tmdb.values()]

            if local_media_ids:
                watched_q = await db.execute(
                    select(WatchEvent.media_id).where(
                        WatchEvent.user_id == current_user.id,
                        WatchEvent.media_id.in_(local_media_ids),
                    ).distinct()
                )
                watched_ep_ids = {r[0] for r in watched_q.all()}

                ep_ratings_q = await db.execute(
                    select(Rating.media_id, Rating.rating).where(
                        Rating.user_id == current_user.id,
                        Rating.media_id.in_(local_media_ids),
                        Rating.season_number.is_(None),
                    )
                )
                episode_ratings = {r[0]: r[1] for r in ep_ratings_q.all()}

            # Fetch list membership per episode
            episode_in_lists: dict[int, list[int]] = {}
            if local_media_ids:
                user_lists_q = await db.execute(
                    select(UserList.id).where(UserList.user_id == current_user.id)
                )
                user_list_ids = [r[0] for r in user_lists_q.all()]
                if user_list_ids:
                    ep_lists_q = await db.execute(
                        select(Media.tmdb_id, ListItem.list_id)
                        .join(ListItem, ListItem.media_id == Media.id)
                        .where(
                            Media.id.in_(local_media_ids),
                            ListItem.list_id.in_(user_list_ids),
                        )
                        .distinct()
                    )
                    for ep_tmdb_id, list_id in ep_lists_q.all():
                        episode_in_lists.setdefault(ep_tmdb_id, []).append(list_id)

            # Merge local library status
            local_map = {ep.episode_number: ep for ep in local_episodes}

            # Subquery to check which (season, episode) pairs are in the user collection.
            # Primary path: match by show_id. The outerjoin also catches rows where show_id
            # points to a different DB row for the same TMDB show (duplicate show rows).
            coll_show_conditions = [ShowModel.tmdb_id == series_tmdb_id]
            if show:
                coll_show_conditions.insert(0, Media.show_id == show.id)
            user_coll_eps_q = await db.execute(
                select(Media.season_number, Media.episode_number)
                .join(Collection, Collection.media_id == Media.id)
                .outerjoin(ShowModel, ShowModel.id == Media.show_id)
                .where(
                    Collection.user_id == current_user.id,
                    Media.media_type == MediaType.episode,
                    Media.season_number == season_number,
                    or_(*coll_show_conditions)
                )
                .distinct()
            )
            user_collected_eps = {(r[0], r[1]) for r in user_coll_eps_q.all()}

            # Fallback: also match by TMDB episode ID for rows where show_id is null.
            if season_ep_tmdb_ids:
                tmdb_id_coll_q = await db.execute(
                    select(Media.season_number, Media.episode_number)
                    .join(Collection, Collection.media_id == Media.id)
                    .where(
                        Collection.user_id == current_user.id,
                        Media.media_type == MediaType.episode,
                        Media.tmdb_id.in_(season_ep_tmdb_ids),
                        Media.show_id.is_(None),
                    )
                    .distinct()
                )
                user_collected_eps |= {(r[0], r[1]) for r in tmdb_id_coll_q.all()}

            episodes = []
            for ep in tmdb_episodes:
                ep_num = ep.get("episode_number")
                local_ep = local_map.get(ep_num)
                local_media_id = local_ep.id if local_ep else None
                
                is_in_library = (season_number, ep_num) in user_collected_eps

                episodes.append(
                    {
                        "id": local_media_id,
                        "tmdb_id": ep.get("id"),
                        "type": "episode",
                        "title": ep.get("name"),
                        "overview": ep.get("overview"),
                        "poster_path": tmdb.poster_url(
                            ep.get("still_path"), size="w500"
                        ),
                        "air_date": ep.get("air_date"),
                        "episode_number": ep_num,
                        "season_number": season_number,
                        "tmdb_rating": ep.get("vote_average"),
                        "in_library": is_in_library,
                        "runtime": ep.get("runtime"),
                        "watched": local_media_id in watched_ep_ids if local_media_id else False,
                        "user_rating": episode_ratings.get(local_media_id) if local_media_id else None,
                        "in_lists": episode_in_lists.get(ep.get("id"), []),
                    }
                )

            show_state: dict = {"tmdb_id": series_tmdb_id, "type": "series"}
            await enrich_with_state(db, current_user.id, [show_state])

            # Season-level stats: watched, in_library, collection_pct, user_rating
            collected_in_season = 0
            if show:
                # Count collected episodes in this season.
                # Primary path: match by show_id.
                coll_q = await db.execute(
                    select(func.count(func.distinct(Media.episode_number)))
                    .join(Collection, Collection.media_id == Media.id)
                    .where(
                        Media.show_id == show.id,
                        Media.season_number == season_number,
                        Collection.user_id == current_user.id,
                        Media.media_type == MediaType.episode,
                        Media.episode_number.isnot(None),
                    )
                )
                collected_in_season = coll_q.scalar_one()

                # Fallback: count any collected episodes matched only by TMDB ID (show_id null).
                # This covers rows created before show_id was reliably set on manual collects.
                if season_ep_tmdb_ids:
                    null_show_coll_q = await db.execute(
                        select(Media.episode_number)
                        .join(Collection, Collection.media_id == Media.id)
                        .where(
                            Collection.user_id == current_user.id,
                            Media.media_type == MediaType.episode,
                            Media.tmdb_id.in_(season_ep_tmdb_ids),
                            Media.show_id.is_(None),
                            Media.episode_number.isnot(None),
                        )
                        .distinct()
                    )
                    null_show_ep_nums = {r[0] for r in null_show_coll_q.all()}
                    
                    # Merge: avoid double-counting episodes already found via show_id.
                    already_by_show_q = await db.execute(
                        select(Media.episode_number)
                        .join(Collection, Collection.media_id == Media.id)
                        .where(
                            Media.show_id == show.id,
                            Media.season_number == season_number,
                            Collection.user_id == current_user.id,
                            Media.media_type == MediaType.episode,
                            Media.episode_number.isnot(None),
                        )
                        .distinct()
                    )
                    already_ep_nums = {r[0] for r in already_by_show_q.all()}
                    extra = null_show_ep_nums - already_ep_nums
                    collected_in_season += len(extra)
            elif season_ep_tmdb_ids:
                # If no local show, just count by TMDB IDs
                coll_q = await db.execute(
                    select(func.count(func.distinct(Media.episode_number)))
                    .join(Collection, Collection.media_id == Media.id)
                    .where(
                        Collection.user_id == current_user.id,
                        Media.media_type == MediaType.episode,
                        Media.tmdb_id.in_(season_ep_tmdb_ids),
                        Media.episode_number.isnot(None),
                    )
                )
                collected_in_season = coll_q.scalar_one()

            season_in_library = collected_in_season > 0
            aired_denom = total_aired_in_season if total_aired_in_season > 0 else total_in_season
            season_collection_pct = min(100, int((collected_in_season / aired_denom) * 100)) if aired_denom > 0 else 0

            # Count unique episodes in this season that have been watched
            # episodes list contains "watched": True/False for each episode.
            watched_count = sum(1 for ep in episodes if ep.get("watched"))
            season_watched = watched_count >= aired_denom if aired_denom > 0 else False
            season_watch_pct = min(100, int((watched_count / aired_denom) * 100)) if aired_denom > 0 else 0

            # Season user rating (stored against show's Media row with season_number)
            season_user_rating = None
            show_media_q = await db.execute(
                select(Media).where(Media.tmdb_id == series_tmdb_id, Media.media_type == MediaType.series)
            )
            show_media = show_media_q.scalar_one_or_none()
            if show_media:
                rating_q = await db.execute(
                    select(Rating.rating).where(
                        Rating.media_id == show_media.id,
                        Rating.user_id == current_user.id,
                        Rating.season_number == season_number,
                    )
                )
                season_user_rating = rating_q.scalar_one_or_none()

            return {
                "id": tmdb_data.get("id"),
                "tmdb_id": series_tmdb_id,
                "season_number": season_number,
                "name": tmdb_data.get("name"),
                "overview": tmdb_data.get("overview"),
                "poster_path": tmdb.poster_url(tmdb_data.get("poster_path")),
                "backdrop_path": tmdb.poster_url(
                    tmdb_data.get("backdrop_path"), size="w1280"
                ),
                "air_date": tmdb_data.get("air_date"),
                "tmdb_rating": tmdb_data.get("vote_average"),
                "episodes": episodes,
                "show": show_info,
                "show_watched": show_state.get("watched", False),
                "season_watched": season_watched,
                "season_watch_pct": season_watch_pct,
                "season_in_library": season_in_library,
                "season_collection_pct": season_collection_pct,
                "season_user_rating": season_user_rating,
                "show_in_lists": show_state.get("in_lists", []),
                "show_in_library": show_state.get("collection_pct", 0) > 0,
                "show_collection_pct": show_state.get("collection_pct", 0),
                "show_request_enabled": show_state.get("request_enabled", False),
                "show_is_monitored": show_state.get("is_monitored", False),
            }
        elif show:
            # Fallback to local only if no TMDB key
            return {
                "season_number": season_number,
                "name": f"Season {season_number}",
                "episodes": [format_media(ep) for ep in local_episodes],
                "show": format_show(show),
            }
        else:
            raise HTTPException(status_code=404, detail="Show not found")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=404, detail=f"Season not found: {e}")


@router.get("/{series_tmdb_id}/season/{season_number}/{episode_number}")
async def get_episode_detail(
    series_tmdb_id: int,
    season_number: int,
    episode_number: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    api_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(api_key):
        raise HTTPException(status_code=404, detail="TMDB API Key not configured")

    try:
        import asyncio

        show_result = await db.execute(
            select(ShowModel).where(ShowModel.tmdb_id == series_tmdb_id)
        )
        show = show_result.scalar_one_or_none()

        if show:
            ep_data = await tmdb.get_episode(
                series_tmdb_id, season_number, episode_number, api_key=api_key
            )
            show_info = format_show(show)
        else:
            ep_data, show_tmdb = await asyncio.gather(
                tmdb.get_episode(
                    series_tmdb_id, season_number, episode_number, api_key=api_key
                ),
                tmdb.get_show(series_tmdb_id, api_key=api_key),
            )
            show_info = {
                "id": None,
                "tmdb_id": series_tmdb_id,
                "title": show_tmdb.get("name"),
                "poster_path": tmdb.poster_url(show_tmdb.get("poster_path")),
                "backdrop_path": tmdb.poster_url(
                    show_tmdb.get("backdrop_path"), size="w1280"
                ),
            }

        # Check local library for this episode
        local_ep = None
        library_info = None
        if show:
            local_result = await db.execute(
                select(Media)
                .where(Media.media_type == MediaType.episode)
                .where(Media.show_id == show.id)
                .where(Media.season_number == season_number)
                .where(Media.episode_number == episode_number)
            )
            local_ep = local_result.scalars().first()
            
            if local_ep:
                coll_q = (
                    select(CollectionFile)
                    .join(Collection, Collection.id == CollectionFile.collection_id)
                    .where(Collection.media_id == local_ep.id, Collection.user_id == current_user.id)
                    .order_by(CollectionFile.added_at.desc())
                )
                coll_res = await db.execute(coll_q)
                coll_file = coll_res.scalars().first()
                if coll_file:
                    library_info = {
                        "resolution": coll_file.resolution,
                        "video_codec": coll_file.video_codec,
                        "audio_codec": coll_file.audio_codec,
                        "audio_channels": coll_file.audio_channels,
                        "audio_languages": coll_file.audio_languages,
                        "subtitle_languages": coll_file.subtitle_languages,
                    }

        credits = ep_data.get("credits", {})
        cast = [
            {
                "tmdb_id": c.get("id"),
                "name": c.get("name"),
                "character": c.get("character"),
                "profile_path": tmdb.poster_url(c.get("profile_path")),
            }
            for c in credits.get("cast", [])[:12]
        ]
        guest_stars = [
            {
                "tmdb_id": c.get("id"),
                "name": c.get("name"),
                "character": c.get("character"),
                "profile_path": tmdb.poster_url(c.get("profile_path")),
            }
            for c in ep_data.get("guest_stars", [])[:6]
        ]

        # Get all episodes in this season for navigation
        season_tmdb = await tmdb.get_season(
            series_tmdb_id, season_number, api_key=api_key
        )
        episodes_nav = [
            {
                "episode_number": ep.get("episode_number"),
                "title": ep.get("name"),
            }
            for ep in season_tmdb.get("episodes", [])
        ]

        ep_tmdb_id = ep_data.get("id")
        ep_state: dict = {"tmdb_id": ep_tmdb_id, "type": "episode"}
        await enrich_with_state(db, current_user.id, [ep_state])

        return {
            "id": local_ep.id if local_ep else None,
            "tmdb_id": ep_tmdb_id,
            "in_library": ep_state.get("in_library", local_ep is not None),
            "watched": ep_state.get("watched", False),
            "in_lists": ep_state.get("in_lists", []),
            "collection_pct": ep_state.get("collection_pct", 100 if local_ep else 0),
            "user_rating": ep_state.get("user_rating"),
            "play_count": ep_state.get("play_count", 0),
            "episode_number": episode_number,
            "season_number": season_number,
            "title": ep_data.get("name"),
            "overview": ep_data.get("overview"),
            "still_path": tmdb.poster_url(ep_data.get("still_path"), size="w780"),
            "air_date": ep_data.get("air_date"),
            "runtime": ep_data.get("runtime"),
            "tmdb_rating": ep_data.get("vote_average"),
            "cast": cast,
            "guest_stars": guest_stars,
            "show": show_info,
            "episodes": episodes_nav,
            "library": library_info,
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=404, detail=f"Episode not found: {e}")


@router.post("/{series_tmdb_id}/refresh")
async def refresh_show_metadata(
    series_tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    api_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(api_key):
        raise HTTPException(status_code=400, detail="TMDB API key not configured")

    show_result = await db.execute(
        select(ShowModel).where(ShowModel.tmdb_id == series_tmdb_id)
    )
    show = show_result.scalar_one_or_none()
    if not show:
        raise HTTPException(status_code=404, detail="Show not found in local library")

    try:
        data = await tmdb.get_show(series_tmdb_id, api_key=api_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TMDB fetch failed: {e}")

    # Update show-level fields
    show.title = data.get("name") or show.title
    show.original_title = data.get("original_name")
    show.overview = data.get("overview")
    show.poster_path = tmdb.poster_url(data.get("poster_path"))
    show.backdrop_path = tmdb.poster_url(data.get("backdrop_path"), size="w1280")
    show.tmdb_rating = data.get("vote_average")
    show.status = data.get("status")
    show.tagline = data.get("tagline")
    show.first_air_date = data.get("first_air_date")
    show.last_air_date = data.get("last_air_date")
    show.tmdb_data = {
        "genres": [g["name"] for g in data.get("genres", [])],
        "external_ids": data.get("external_ids", {}),
        "original_language": data.get("original_language"),
        "seasons": [
            {
                "season_number": s["season_number"],
                "poster_path": tmdb.poster_url(s.get("poster_path")),
                "episode_count": s["episode_count"],
                "name": s["name"],
                "air_date": s.get("air_date"),
                "overview": s.get("overview"),
            }
            for s in data.get("seasons", [])
        ],
    }

    # Re-enrich all local episodes linked to this show
    ep_result = await db.execute(
        select(Media)
        .where(Media.media_type == MediaType.episode)
        .where(Media.show_id == show.id)
    )
    episodes = ep_result.scalars().all()

    # Also find orphaned episodes (show_id = null) in this user's collection
    # whose (season_number, episode_number) match TMDB data for this show.
    # This happens when the show's TMDB lookup failed during the original sync,
    # leaving episodes with no show_id even though they belong to this show.
    user_media_ids_sq = select(Collection.media_id).where(Collection.user_id == current_user.id)
    orphan_result = await db.execute(
        select(Media)
        .where(
            Media.media_type == MediaType.episode,
            Media.show_id.is_(None),
            Media.id.in_(user_media_ids_sq),
        )
    )
    orphans = orphan_result.scalars().all()

    semaphore = asyncio.Semaphore(10)
    season_data: dict[int, dict[int, dict]] = {}

    # Collect seasons needed: linked episodes + all show seasons (for orphan matching)
    linked_seasons = {ep.season_number for ep in episodes if ep.season_number is not None}
    tmdb_seasons = {s["season_number"] for s in data.get("seasons", [])}
    orphan_seasons = {ep.season_number for ep in orphans if ep.season_number is not None}
    # Only fetch seasons that could contain orphans belonging to this show
    needed_seasons = linked_seasons | (tmdb_seasons & orphan_seasons)

    async def fetch_season(sn: int) -> None:
        async with semaphore:
            try:
                d = await tmdb.get_season(series_tmdb_id, sn, api_key=api_key)
                season_data[sn] = {ep["episode_number"]: ep for ep in d.get("episodes", [])}
            except Exception:
                season_data[sn] = {}

    if needed_seasons:
        await asyncio.gather(*[fetch_season(sn) for sn in needed_seasons])

    def apply_episode_data(media: Media, ep: dict) -> None:
        media.show_id = show.id
        media.tmdb_id = ep.get("id") or media.tmdb_id
        media.title = ep.get("name") or media.title
        media.overview = ep.get("overview")
        media.poster_path = tmdb.poster_url(ep.get("still_path"), size="w500")
        media.release_date = ep.get("air_date")
        media.tmdb_rating = ep.get("vote_average")
        media.tmdb_data = {"runtime": ep.get("runtime"), "cast": []}

    for media in episodes:
        if media.season_number is None:
            continue
        ep = season_data.get(media.season_number, {}).get(media.episode_number)
        if ep:
            apply_episode_data(media, ep)

    # Adopt orphans whose (season, episode) has exactly one candidate in this show's TMDB data
    for media in orphans:
        if media.season_number is None or media.episode_number is None:
            continue
        ep = season_data.get(media.season_number, {}).get(media.episode_number)
        if ep:
            apply_episode_data(media, ep)

    all_media_ids = [ep.id for ep in episodes] + [ep.id for ep in orphans]
    await refresh_technical_data(db, all_media_ids, current_user.id)

    await db.commit()
    return {"message": "Metadata refreshed successfully"}


# ── TVDB Show Endpoints ─────────────────────────────────────────────────────


@router.get("/tvdb/{tvdb_id}")
async def get_tvdb_show(
    tvdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    api_key = await get_user_tvdb_key(db, current_user.id)
    if not api_key:
        raise HTTPException(status_code=400, detail="TVDB API key not configured")

    try:
        raw = await tvdb_client.get_series(tvdb_id, api_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TVDB fetch failed: {e}")

    show_data = tvdb_client.format_series(raw)
    cast = tvdb_client.format_cast(raw)

    # Look up local Show row by tvdb_id
    show_result = await db.execute(select(ShowModel).where(ShowModel.tvdb_id == tvdb_id))
    show = show_result.scalar_one_or_none()

    # Collect per-season library state if we have a local show
    season_states: dict = {}
    if show:
        coll_per_season_q = await db.execute(
            select(Media.season_number, func.count(func.distinct(Media.episode_number)))
            .join(Collection, Collection.media_id == Media.id)
            .where(
                Media.show_id == show.id,
                Collection.user_id == current_user.id,
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.episode_number.isnot(None),
            )
            .group_by(Media.season_number)
        )
        coll_per_season: dict = dict(coll_per_season_q.all())

        watched_per_season_q = await db.execute(
            select(Media.season_number, func.count(func.distinct(Media.episode_number)))
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(
                Media.show_id == show.id,
                WatchEvent.user_id == current_user.id,
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.episode_number.isnot(None),
            )
            .group_by(Media.season_number)
        )
        watched_per_season: dict = dict(watched_per_season_q.all())

        season_ep_counts = {s["season_number"]: s.get("episode_count", 0) for s in show_data["seasons"]}

        for sn in set(list(coll_per_season.keys()) + list(season_ep_counts.keys())):
            collected = coll_per_season.get(sn, 0)
            watched = watched_per_season.get(sn, 0)
            total = season_ep_counts.get(sn, 0)
            # When TVDB reports 0 episodes (common for web series), use collected count as denominator
            effective_total = total if total > 0 else collected
            season_states[sn] = {
                "in_library": collected > 0,
                "collection_pct": min(100, int((collected / effective_total) * 100)) if effective_total > 0 else 0,
                "watched": watched >= effective_total if effective_total > 0 else False,
                "watch_pct": min(100, int((watched / effective_total) * 100)) if effective_total > 0 else 0,
                "user_rating": None,
            }

    in_library = bool(season_states and any(v["in_library"] for v in season_states.values()))

    # Derive overall watched / collection_pct from season states (skip season 0 specials)
    lib_seasons = [v for sn, v in season_states.items() if sn != 0 and v["in_library"]]
    watched_overall = bool(lib_seasons) and all(v["watched"] for v in lib_seasons)
    total_pct = sum(v["collection_pct"] for sn, v in season_states.items() if sn != 0)
    non_special_seasons = len([sn for sn in season_states if sn != 0])
    collection_pct = int(total_pct / non_special_seasons) if non_special_seasons else 0
    total_watch_pct = sum(v["watch_pct"] for sn, v in season_states.items() if sn != 0)
    watch_pct = int(total_watch_pct / non_special_seasons) if non_special_seasons else 0

    # Sonarr state
    gs = await _get_global_settings(db)
    settings_q = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_q.scalar_one_or_none()
    sonarr_cfg = _effective_sonarr(settings, gs)
    is_monitored = False
    request_enabled = sonarr_cfg is not None
    if sonarr_cfg:
        try:
            import httpx as _httpx
            url = sonarr_cfg.sonarr_url.rstrip("/")
            async with _httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(
                    f"{url}/api/v3/series/lookup",
                    params={"apiKey": sonarr_cfg.sonarr_token, "term": f"tvdb:{tvdb_id}"},
                )
                if res.status_code == 200:
                    for entry in res.json():
                        if entry.get("id"):
                            is_monitored = True
                            break
        except Exception:
            pass

    # Where to watch (local media servers only; no TMDB streaming for TVDB-only shows)
    where_to_watch = await get_where_to_watch(db, current_user.id, tvdb_id, MediaType.series, show=show) if show else []

    # Networks (name only; TVDB doesn't provide logos)
    networks = [{"id": None, "name": n.get("name"), "logo_path": None, "origin_country": None}
                for n in (raw.get("networks") or []) if n.get("name")]

    return {
        **show_data,
        "id": show.id if show else None,
        "tmdb_id": None,
        "type": "series",
        "tagline": None,
        "tmdb_rating": None,
        "imdb_id": show_data.get("imdb_id"),
        "tmdb_id_cross": show_data.get("tmdb_id_cross"),
        "adult": False,
        "in_library": in_library,
        "watched": watched_overall,
        "in_lists": [],
        "collection_pct": collection_pct,
        "watch_pct": watch_pct,
        "is_monitored": is_monitored,
        "request_enabled": request_enabled,
        "request_status": None,
        "user_rating": None,
        "season_states": season_states,
        "seasons": {},
        "seasons_meta": show_data["seasons"],
        "cast": cast,
        "networks": networks,
        "where_to_watch": where_to_watch,
    }


@router.get("/tvdb/{tvdb_id}/season/{season_number}")
async def get_tvdb_season(
    tvdb_id: int,
    season_number: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    api_key = await get_user_tvdb_key(db, current_user.id)
    if not api_key:
        raise HTTPException(status_code=400, detail="TVDB API key not configured")

    try:
        raw_series, raw_episodes = await asyncio.gather(
            tvdb_client.get_series(tvdb_id, api_key),
            tvdb_client.get_series_episodes(tvdb_id, season_number, api_key),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TVDB fetch failed: {e}")

    show_data = tvdb_client.format_series(raw_series)
    eps = [tvdb_client.format_episode(e) for e in raw_episodes]

    # Look up local Show row and episode states
    show_result = await db.execute(select(ShowModel).where(ShowModel.tvdb_id == tvdb_id))
    show = show_result.scalar_one_or_none()

    watched_ep_ids: set = set()
    episode_ratings: dict = {}
    user_collected_eps: set = set()

    if show:
        ep_result = await db.execute(
            select(Media)
            .where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number == season_number,
            )
        )
        local_eps = ep_result.scalars().all()
        local_media_ids = [m.id for m in local_eps]

        if local_media_ids:
            watched_q = await db.execute(
                select(WatchEvent.media_id)
                .where(WatchEvent.user_id == current_user.id, WatchEvent.media_id.in_(local_media_ids))
                .distinct()
            )
            watched_ep_ids = {r[0] for r in watched_q.all()}

            ep_ratings_q = await db.execute(
                select(Rating.media_id, Rating.rating)
                .where(
                    Rating.user_id == current_user.id,
                    Rating.media_id.in_(local_media_ids),
                    Rating.season_number.is_(None),
                )
            )
            episode_ratings = {r[0]: r[1] for r in ep_ratings_q.all()}

            coll_q = await db.execute(
                select(Media.episode_number)
                .join(Collection, Collection.media_id == Media.id)
                .where(
                    Media.show_id == show.id,
                    Media.season_number == season_number,
                    Collection.user_id == current_user.id,
                    Media.media_type == MediaType.episode,
                    Media.episode_number.isnot(None),
                )
                .distinct()
            )
            user_collected_eps = {r[0] for r in coll_q.all()}

        # Build episode_number → local Media map
        local_ep_map = {m.episode_number: m for m in local_eps}
    else:
        local_ep_map = {}

    enriched_eps = []
    for ep in eps:
        ep_num = ep.get("episode_number")
        local_m = local_ep_map.get(ep_num)
        enriched_eps.append({
            **ep,
            "id": local_m.id if local_m else None,
            "in_library": ep_num in user_collected_eps,
            "watched": local_m.id in watched_ep_ids if local_m else False,
            "user_rating": episode_ratings.get(local_m.id) if local_m else None,
            "in_lists": [],
        })

    season_meta = next((s for s in show_data["seasons"] if s["season_number"] == season_number), {})

    # Compute season-level stats
    season_in_library = bool(user_collected_eps)
    total_eps = len(eps)
    effective_total = total_eps if total_eps > 0 else len(user_collected_eps)
    season_collection_pct = min(100, int((len(user_collected_eps) / effective_total) * 100)) if effective_total > 0 else 0
    season_watched = bool(local_eps) and len(watched_ep_ids) >= len(local_eps) if show else False
    season_watch_pct = min(100, int((len(watched_ep_ids) / effective_total) * 100)) if effective_total > 0 else 0

    return {
        "tvdb_id": tvdb_id,
        "season_number": season_number,
        "name": season_meta.get("name") or f"Season {season_number}",
        "overview": season_meta.get("overview"),
        "poster_path": season_meta.get("poster_path"),
        "backdrop_path": show_data["backdrop_path"],
        "air_date": season_meta.get("air_date"),
        "episodes": enriched_eps,
        "season_in_library": season_in_library,
        "season_watched": season_watched,
        "season_watch_pct": season_watch_pct,
        "season_collection_pct": season_collection_pct,
        "season_user_rating": None,
        "show_in_library": show is not None,
        "show": {
            "id": show.id if show else None,
            "tvdb_id": tvdb_id,
            "title": show_data["title"],
            "poster_path": show_data["poster_path"],
            "backdrop_path": show_data["backdrop_path"],
            "seasons_meta": show_data["seasons"],
        },
    }


@router.get("/tvdb/{tvdb_id}/season/{season_number}/episode/{episode_number}")
async def get_tvdb_episode(
    tvdb_id: int,
    season_number: int,
    episode_number: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    api_key = await get_user_tvdb_key(db, current_user.id)
    if not api_key:
        raise HTTPException(status_code=400, detail="TVDB API key not configured")

    try:
        raw_series, raw_episodes = await asyncio.gather(
            tvdb_client.get_series(tvdb_id, api_key),
            tvdb_client.get_series_episodes(tvdb_id, season_number, api_key),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TVDB fetch failed: {e}")

    show_data = tvdb_client.format_series(raw_series)
    eps = [tvdb_client.format_episode(e) for e in raw_episodes]
    ep_data = next((e for e in eps if e.get("episode_number") == episode_number), None)
    if not ep_data:
        raise HTTPException(status_code=404, detail="Episode not found")

    show_result = await db.execute(select(ShowModel).where(ShowModel.tvdb_id == tvdb_id))
    show = show_result.scalar_one_or_none()

    in_library = False
    watched = False
    user_rating = None
    local_ep_id = None
    library_info = None
    play_count = 0

    if show:
        local_ep_q = await db.execute(
            select(Media).where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number == season_number,
                Media.episode_number == episode_number,
            )
        )
        local_ep = local_ep_q.scalars().first()
        if local_ep:
            local_ep_id = local_ep.id
            coll_q = await db.execute(
                select(func.count()).select_from(Collection).where(
                    Collection.media_id == local_ep.id,
                    Collection.user_id == current_user.id,
                )
            )
            in_library = coll_q.scalar_one() > 0
            watched_q = await db.execute(
                select(func.count()).select_from(WatchEvent).where(
                    WatchEvent.media_id == local_ep.id,
                    WatchEvent.user_id == current_user.id,
                )
            )
            play_count = watched_q.scalar_one()
            watched = play_count > 0
            rating_q = await db.execute(
                select(Rating.rating).where(
                    Rating.media_id == local_ep.id,
                    Rating.user_id == current_user.id,
                    Rating.season_number.is_(None),
                )
            )
            user_rating = rating_q.scalar_one_or_none()

            if in_library:
                coll_file_q = await db.execute(
                    select(CollectionFile)
                    .join(Collection, Collection.id == CollectionFile.collection_id)
                    .where(
                        Collection.media_id == local_ep.id,
                        Collection.user_id == current_user.id,
                    )
                    .order_by(CollectionFile.added_at.desc())
                )
                coll_file = coll_file_q.scalars().first()
                if coll_file:
                    library_info = {
                        "resolution": coll_file.resolution,
                        "video_codec": coll_file.video_codec,
                        "audio_codec": coll_file.audio_codec,
                        "audio_channels": coll_file.audio_channels,
                        "audio_languages": coll_file.audio_languages,
                        "subtitle_languages": coll_file.subtitle_languages,
                    }

    cast = tvdb_client.format_cast(raw_series)
    season_meta = next((s for s in show_data["seasons"] if s["season_number"] == season_number), {})

    return {
        **ep_data,
        "id": local_ep_id,
        "in_library": in_library,
        "watched": watched,
        "user_rating": user_rating,
        "play_count": play_count,
        "in_lists": [],
        "library": library_info,
        "cast": cast,
        "episodes": [{"episode_number": e["episode_number"], "name": e["name"]} for e in eps],
        "show": {
            "id": show.id if show else None,
            "tvdb_id": tvdb_id,
            "title": show_data["title"],
            "poster_path": show_data["poster_path"],
            "backdrop_path": show_data["backdrop_path"],
        },
        "season": {
            "name": season_meta.get("name") or f"Season {season_number}",
            "season_number": season_number,
            "poster_path": season_meta.get("poster_path"),
        },
    }


@router.post("/tvdb/{tvdb_id}/refresh")
async def refresh_tvdb_show_metadata(
    tvdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    api_key = await get_user_tvdb_key(db, current_user.id)
    if not api_key:
        raise HTTPException(status_code=400, detail="TVDB API key not configured")

    show_result = await db.execute(select(ShowModel).where(ShowModel.tvdb_id == tvdb_id))
    show = show_result.scalar_one_or_none()
    if not show:
        raise HTTPException(status_code=404, detail="Show not found in local library")

    try:
        raw = await tvdb_client.get_series(tvdb_id, api_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TVDB fetch failed: {e}")

    show_fmt = tvdb_client.format_series(raw)
    show.title = show_fmt["title"] or show.title
    show.original_title = show_fmt.get("original_title")
    show.overview = show_fmt.get("overview")
    show.poster_path = show_fmt.get("poster_path")
    show.backdrop_path = show_fmt.get("backdrop_path")
    show.status = show_fmt.get("status")
    show.first_air_date = show_fmt.get("first_air_date")
    show.last_air_date = show_fmt.get("last_air_date")
    show.tmdb_data = {
        **(show.tmdb_data or {}),
        "seasons": show_fmt.get("seasons", []),
        "genres": show_fmt.get("genres", []),
        "source": "tvdb",
    }
    await db.commit()
    return {"message": "Metadata refreshed successfully"}
