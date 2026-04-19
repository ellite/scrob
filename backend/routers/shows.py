import asyncio
from datetime import datetime, date
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast as sa_cast, Text, or_, and_

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
from routers.media import format_media, get_user_tmdb_key, check_tmdb_key, enrich_with_state, refresh_technical_data

from dependencies import get_current_user
from core import tmdb

router = APIRouter()


def format_show(show: ShowModel) -> dict:
    return {
        "id": show.id,
        "tmdb_id": show.tmdb_id,
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
        "genres": (show.tmdb_data or {}).get("genres", []),
        "seasons_meta": (show.tmdb_data or {}).get("seasons", []),
        "original_language": (show.tmdb_data or {}).get("original_language"),
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

    # A show is "in the user's collection" if they have at least one episode collected
    user_show_ids = (
        select(Media.show_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == current_user.id, Media.show_id.isnot(None))
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

        # Collected episodes per season
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

        # Watched episodes per season (including those not in collection)
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

        # Season user ratings (stored against the show's Media row with season_number)
        show_media_q = await db.execute(
            select(Media).where(Media.tmdb_id == series_tmdb_id, Media.media_type == MediaType.series)
        )
        show_media = show_media_q.scalar_one_or_none()
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
                "collection_pct": int((collected / total) * 100) if total > 0 else 0,
                "watched": watched >= total if total > 0 else False,
                "user_rating": season_ratings.get(sn),
            }

        # Enhance seasons_meta with TMDB season IDs and ratings from the live TMDB call
        tmdb_season_map: dict = {}
        if tmdb_extra:
            tmdb_season_map = {s["season_number"]: s for s in tmdb_extra.get("seasons", [])}
        base_seasons_meta = (show.tmdb_data or {}).get("seasons", [])
        enhanced_seasons_meta = [
            {
                **s,
                "tmdb_season_id": tmdb_season_map.get(s["season_number"], {}).get("id"),
                "tmdb_rating": tmdb_season_map.get(s["season_number"], {}).get("vote_average"),
            }
            for s in base_seasons_meta
        ]

        return {
            **format_show(show),
            "seasons_meta": enhanced_seasons_meta,
            "original_language": (show.tmdb_data or {}).get("original_language") or (tmdb_extra or {}).get("original_language"),
            "in_library": state_item.get("collection_pct", 0) > 0 if state_item else False,
            "watched": state_item.get("watched", False) if state_item else False,
            "in_lists": state_item.get("in_lists", []),
            "collection_pct": state_item.get("collection_pct", 0),
            "is_monitored": state_item.get("is_monitored", False),
            "request_enabled": state_item.get("request_enabled", False),
            "user_rating": state_item.get("user_rating"),
            "season_states": season_states,
            "seasons": {f"season_{k}": v for k, v in sorted(seasons.items())},
            "cast": cast,
            "networks": networks,
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
            "in_library": state_item_tmdb.get("collection_pct", 0) > 0,
            "watched": state_item_tmdb.get("watched", False),
            "in_lists": state_item_tmdb.get("in_lists", []),
            "collection_pct": state_item_tmdb.get("collection_pct", 0),
            "is_monitored": state_item_tmdb.get("is_monitored", False),
            "request_enabled": state_item_tmdb.get("request_enabled", False),
            "user_rating": state_item_tmdb.get("user_rating"),
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
            season_collection_pct = int((collected_in_season / total_in_season) * 100) if total_in_season > 0 else 0

            # Count unique episodes in this season that have been watched
            # episodes list contains "watched": True/False for each episode.
            watched_count = sum(1 for ep in episodes if ep.get("watched"))
            season_watched = watched_count >= total_in_season if total_in_season > 0 else False

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

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    if settings:
        all_media_ids = [ep.id for ep in episodes] + [ep.id for ep in orphans]
        await refresh_technical_data(db, all_media_ids, settings)

    await db.commit()
    return {"message": "Metadata refreshed successfully"}
