import asyncio
import httpx
import re
import urllib.parse
import uuid
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from starlette.background import BackgroundTask
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_, func, cast as sa_cast, Text
from sqlalchemy.orm import joinedload

from db import get_db
from models.media import Media
from models.collection import Collection, CollectionFile
from models.connections import MediaServerConnection
from models.events import WatchEvent
from models.ratings import Rating
from models.base import MediaType, CollectionSource
from models.lists import List as UserList, ListItem
from models.profile import UserProfileData
from core import tmdb
from dependencies import get_current_user
from models.users import User, UserSettings
from models.show import Show as ShowModel

router = APIRouter()


class SessionReportRequest(BaseModel):
    connection_id: int
    state: str  # "playing" | "progress" | "paused" | "stopped"
    position_ms: int = 0
    duration_ms: int = 0


# Simple TTL cache for the /for-you endpoint — keyed by user_id
import time as _time
_FOR_YOU_CACHE: dict[int, tuple[float, dict]] = {}
_FOR_YOU_TTL = 900  # 15 minutes

# TMDB genre name → ID mappings (used to convert filter names to discover API IDs)
MOVIE_GENRE_IDS: dict[str, int] = {
    "Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35,
    "Crime": 80, "Documentary": 99, "Drama": 18, "Family": 10751,
    "Fantasy": 14, "History": 36, "Horror": 27, "Music": 10402,
    "Mystery": 9648, "Romance": 10749, "Science Fiction": 878,
    "Thriller": 53, "War": 10752, "Western": 37,
}

TV_GENRE_IDS: dict[str, int] = {
    "Action & Adventure": 10759, "Animation": 16, "Comedy": 35,
    "Crime": 80, "Documentary": 99, "Drama": 18, "Family": 10751,
    "Kids": 10762, "Mystery": 9648, "News": 10763, "Reality": 10764,
    "Sci-Fi & Fantasy": 10765, "Soap": 10766, "Talk": 10767,
    "War & Politics": 10768, "Western": 37,
}

TV_STATUS_IDS: dict[str, int] = {
    "Returning Series": 0, "Planned": 1, "In Production": 2,
    "Ended": 3, "Canceled": 4,
}


async def enrich_with_state(
    db: AsyncSession,
    user_id: int,
    items: list[dict],
) -> list[dict]:
    """Add watched, in_lists, collection_pct, and is_monitored fields to a list of media items."""
    movie_tmdb_ids = [i["tmdb_id"] for i in items if i.get("type") == "movie" and i.get("tmdb_id")]
    show_tmdb_ids  = [i["tmdb_id"] for i in items if i.get("type") == "series" and i.get("tmdb_id")]
    ep_tmdb_ids    = [i["tmdb_id"] for i in items if i.get("type") == "episode" and i.get("tmdb_id")]
    all_tmdb_ids   = [i["tmdb_id"] for i in items if i.get("tmdb_id")]

    if not all_tmdb_ids:
        return items

    # --- Radarr / Sonarr state (Request button logic) ---
    settings_q = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    settings = settings_q.scalar_one_or_none()
    
    monitored_status = {} # tmdb_id -> bool
    request_enabled_map = {} # tmdb_id -> bool
    
    if settings:
        radarr_ready = all([settings.radarr_url, settings.radarr_token, settings.radarr_root_folder, settings.radarr_quality_profile])
        sonarr_ready = all([settings.sonarr_url, settings.sonarr_token, settings.sonarr_root_folder, settings.sonarr_quality_profile])
        
        # We only do the expensive lookup if len(items) == 1 (detail view)
        if len(items) == 1:
            item = items[0]
            tid = item.get("tmdb_id")
            t = item.get("type")
            
            from core import radarr, sonarr
            
            if t == "movie":
                request_enabled_map[tid] = radarr_ready
                if radarr_ready:
                    try:
                        url = settings.radarr_url.rstrip("/")
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            res = await client.get(
                                f"{url}/api/v3/movie/lookup",
                                params={"apiKey": settings.radarr_token, "term": f"tmdb:{tid}"}
                            )
                            if res.status_code == 200:
                                lookup = res.json()
                                if lookup:
                                    # If any result has a non-zero id, it's already in Radarr
                                    for entry in lookup:
                                        if entry.get("id"):
                                            monitored_status[tid] = True
                                            break
                    except Exception: pass
            
            elif t == "series":
                request_enabled_map[tid] = sonarr_ready
                if sonarr_ready:
                    try:
                        # Get TVDB ID: prefer cached value in Show.tmdb_data (no API call),
                        # fall back to live TMDB lookup only if missing.
                        tvdb_id: int | None = None
                        show_q = await db.execute(
                            select(ShowModel).where(ShowModel.tmdb_id == tid)
                        )
                        show_row = show_q.scalar_one_or_none()
                        if show_row and show_row.tmdb_data:
                            tvdb_id = (show_row.tmdb_data.get("external_ids") or {}).get("tvdb_id")

                        if not tvdb_id:
                            from core import tmdb as tmdb_core
                            tmdb_key = await get_user_tmdb_key(db, user_id)
                            ext_ids = await tmdb_core.get_external_ids(tid, "tv", api_key=tmdb_key)
                            tvdb_id = ext_ids.get("tvdb_id")

                        if tvdb_id:
                            url = settings.sonarr_url.rstrip("/")
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                res = await client.get(
                                    f"{url}/api/v3/series/lookup",
                                    params={"apiKey": settings.sonarr_token, "term": f"tvdb:{tvdb_id}"}
                                )
                                if res.status_code == 200:
                                    lookup = res.json()
                                    if lookup:
                                        # If any result has a non-zero id, it's already in Sonarr
                                        for entry in lookup:
                                            if entry.get("id"):
                                                monitored_status[tid] = True
                                                break
                    except Exception: pass
        else:
            # For lists, just set if the button should be allowed (optional, currently cards don't show it)
            for item in items:
                tid = item.get("tmdb_id")
                t = item.get("type")
                if t == "movie": request_enabled_map[tid] = radarr_ready
                elif t == "series": request_enabled_map[tid] = sonarr_ready

    # --- Watched state ---
    watched_movies: set[int] = set()
    if movie_tmdb_ids:
        q = await db.execute(
            select(Media.tmdb_id)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(WatchEvent.user_id == user_id, Media.tmdb_id.in_(movie_tmdb_ids), Media.media_type == MediaType.movie)
            .distinct()
        )
        watched_movies = {r[0] for r in q.all()}

    watched_shows: set[int] = set()
    show_watched_count_map: dict[int, int] = {}
    if show_tmdb_ids:
        # Count distinct watched episodes per show, deduplicated by (season, episode).
        # Use a join on ShowModel by tmdb_id to group episodes by their show's TMDB ID.
        # This handles cases where multiple Show rows might exist for the same TMDB ID.
        # Count distinct watched episodes per show, deduplicated by (season, episode).
        # We need to find all watched episodes for these shows. 
        # Most episodes will be linked via Media.show_id -> ShowModel.id -> ShowModel.tmdb_id.
        # But some might have a null show_id. We can find those by matching their TMDB ID
        # if we know which episode TMDB IDs belong to which show. 
        # To keep it efficient and avoid extra TMDB lookups, let's use the show_id join
        # but also allow matching by show_id directly if we have the local Show IDs.
        
        # 1. Get local Show IDs for the TMDB IDs we are interested in.
        show_id_map_q = await db.execute(
            select(ShowModel.tmdb_id, ShowModel.id)
            .where(ShowModel.tmdb_id.in_(show_tmdb_ids))
        )
        show_tmdb_to_local_id = {r[0]: r[1] for r in show_id_map_q.all()}
        local_show_ids = list(show_tmdb_to_local_id.values())

        watched_eps_sq = (
            select(ShowModel.tmdb_id.label("show_tmdb_id"), Media.season_number, Media.episode_number)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .join(ShowModel, ShowModel.id == Media.show_id)
            .where(
                WatchEvent.user_id == user_id,
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.season_number != 0,
                Media.episode_number.isnot(None),
                ShowModel.tmdb_id.in_(show_tmdb_ids),
            )
            .group_by(ShowModel.tmdb_id, Media.season_number, Media.episode_number)
            .subquery()
        )
        watched_count_q = await db.execute(
            select(watched_eps_sq.c.show_tmdb_id, func.count())
            .group_by(watched_eps_sq.c.show_tmdb_id)
        )
        show_watched_count_map = {r[0]: r[1] for r in watched_count_q.all()}

        # 2. Add episodes that might have a null show_id but are watched.
        # This is harder without knowing episode TMDB IDs. 
        # But if the user marked them watched via Scrob, they SHOULD have show_id set.
        # Let's check if there are any episodes with null show_id that belong to these shows.
        # Actually, let's just make the existing logic more robust by ensuring show_id is set
        # when marking as watched (which we already do in history.py).

    watched_episodes: set[int] = set()
    if ep_tmdb_ids:
        q = await db.execute(
            select(Media.tmdb_id)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(WatchEvent.user_id == user_id, Media.tmdb_id.in_(ep_tmdb_ids), Media.media_type == MediaType.episode)
            .distinct()
        )
        watched_episodes = {r[0] for r in q.all()}

    # --- List membership ---
    user_list_ids_q = await db.execute(select(UserList.id).where(UserList.user_id == user_id))
    user_list_ids = [r[0] for r in user_list_ids_q.all()]

    list_membership: dict[int, list[int]] = {}
    if user_list_ids and all_tmdb_ids:
        q = await db.execute(
            select(Media.tmdb_id, ListItem.list_id)
            .join(ListItem, ListItem.media_id == Media.id)
            .where(ListItem.list_id.in_(user_list_ids), Media.tmdb_id.in_(all_tmdb_ids))
            .distinct()
        )
        for row_tmdb_id, list_id in q.all():
            list_membership.setdefault(row_tmdb_id, []).append(list_id)

    # --- Collection pct and watched status for shows ---
    show_pct: dict[int, int] = {}
    show_aired_count: dict[int, int] = {}
    if show_tmdb_ids:
        # Total episodes from TMDB metadata.
        # Check local DB first for existing show rows.
        shows_meta_q = await db.execute(
            select(ShowModel.tmdb_id, ShowModel.tmdb_data, ShowModel.status)
            .where(ShowModel.tmdb_id.in_(show_tmdb_ids))
        )
        total_map: dict[int, int] = {}
        show_status_map: dict[int, str] = {}
        show_seasons_map: dict[int, list] = {}
        show_ep_tmdb_ids: dict[int, set[int]] = {} # show_tmdb_id -> {ep_tmdb_id, ...}

        for show_tmdb_id, tmdb_data, status in shows_meta_q.all():
            seasons = (tmdb_data or {}).get("seasons", [])
            show_status_map[show_tmdb_id] = status or ""
            show_seasons_map[show_tmdb_id] = seasons
            total_map[show_tmdb_id] = sum(
                s.get("episode_count", 0) for s in seasons if s.get("season_number", 0) != 0
            )

        # 2. For shows not in local DB (or to ensure accuracy), fetch details from TMDB
        missing_show_ids = [tid for tid in show_tmdb_ids if tid not in total_map]
        
        # We also need to get ALL episode TMDB IDs for these shows to correctly identify
        # watched episodes that might not have a show_id link.
        # This is expensive, so we only do it if the user has watched episodes.
        tmdb_key = await get_user_tmdb_key(db, user_id)
        if show_tmdb_ids and check_tmdb_key(tmdb_key):
            async def fetch_show_and_seasons(tid: int):
                try:
                    data = await tmdb.get_show(tid, api_key=tmdb_key)
                    ep_ids = set()
                    # We need to fetch each season to get the episode IDs.
                    # This is too slow for 20 shows. 
                    # INSTEAD: Let's use the show_id join but ALSO try to match by TMDB ID 
                    # if we can find which episodes belong to which show.
                    # Actually, mark_show_watched ALREADY ENSURES show_id is set.
                    # If it's NOT set, it's a legacy or sync issue.
                    return tid, data, ep_ids
                except Exception:
                    return tid, None, set()

            if missing_show_ids:
                missing_results = await asyncio.gather(*[fetch_show_and_seasons(tid) for tid in missing_show_ids])
                for tid, data, _ in missing_results:
                    if data:
                        seasons = data.get("seasons", [])
                        show_status_map[tid] = data.get("status", "")
                        show_seasons_map[tid] = seasons
                        total_map[tid] = sum(
                            s.get("episode_count", 0) for s in seasons if s.get("season_number", 0) != 0
                        )

        # Count distinct watched episodes per show, deduplicated by (season, episode).
        # We find episodes by their show_id link. 
        # Primary path: show_id -> ShowModel.id -> ShowModel.tmdb_id
        watched_eps_sq = (
            select(ShowModel.tmdb_id.label("show_tmdb_id"), Media.season_number, Media.episode_number)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .join(ShowModel, ShowModel.id == Media.show_id)
            .where(
                WatchEvent.user_id == user_id,
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.season_number != 0,
                Media.episode_number.isnot(None),
                ShowModel.tmdb_id.in_(show_tmdb_ids),
            )
            .group_by(ShowModel.tmdb_id, Media.season_number, Media.episode_number)
            .subquery()
        )
        watched_count_q = await db.execute(
            select(watched_eps_sq.c.show_tmdb_id, func.count())
            .group_by(watched_eps_sq.c.show_tmdb_id)
        )
        show_watched_count_map = {r[0]: r[1] for r in watched_count_q.all()}

        # Count distinct collected episodes per show
        ep_dedup_sq = (
            select(ShowModel.tmdb_id.label("show_tmdb_id"), Media.season_number, Media.episode_number)
            .join(Collection, Collection.media_id == Media.id)
            .join(ShowModel, ShowModel.id == Media.show_id)
            .where(
                Collection.user_id == user_id,
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.season_number != 0,
                Media.episode_number.isnot(None),
                ShowModel.tmdb_id.in_(show_tmdb_ids),
            )
            .group_by(ShowModel.tmdb_id, Media.season_number, Media.episode_number)
            .subquery()
        )
        collected_q = await db.execute(
            select(ep_dedup_sq.c.show_tmdb_id, func.count())
            .group_by(ep_dedup_sq.c.show_tmdb_id)
        )
        collected_map = {r[0]: r[1] for r in collected_q.all()}

        for tmdb_id in show_tmdb_ids:
            total = total_map.get(tmdb_id, 0)
            collected = collected_map.get(tmdb_id, 0)
            show_pct[tmdb_id] = int((collected / total) * 100) if total > 0 else 0

        # --- Aired counts for 'watched' logic ---
        show_aired_count = {tid: total_map.get(tid, 0) for tid in show_tmdb_ids}

        # For shows between 0–100% that are still active (not Ended/Canceled), the stored
        # episode_count includes unaired episodes. Make parallelised live TMDB calls to get
        # last_episode_to_air and calculate against actually-aired episodes only.
        # If a caller already has last_episode_to_air (e.g. detail page), it can pre-populate
        # _last_episode_to_air on the item to skip the redundant fetch.
        prefetched: dict[int, dict] = {
            item["tmdb_id"]: item["_last_episode_to_air"]
            for item in items
            if item.get("type") == "series" and item.get("_last_episode_to_air")
        }
        FINAL_STATUSES = {"Ended", "Canceled"}
        needs_live_call = [
            tid for tid in show_tmdb_ids
            if tid not in prefetched
            and (0 < show_pct.get(tid, 0) < 100 or 0 < show_watched_count_map.get(tid, 0))
            and show_status_map.get(tid, "") not in FINAL_STATUSES
        ]
        if needs_live_call:
            tmdb_key = await get_user_tmdb_key(db, user_id)
            if check_tmdb_key(tmdb_key):
                async def fetch_last_aired(tid: int) -> tuple[int, dict | None]:
                    try:
                        return tid, await tmdb.get_show_light(tid, api_key=tmdb_key)
                    except Exception:
                        return tid, None

                live_results = await asyncio.gather(*[fetch_last_aired(tid) for tid in needs_live_call])
                for tid, data in live_results:
                    if data and data.get("last_episode_to_air"):
                        prefetched[tid] = data["last_episode_to_air"]

        for tid, last_ep in prefetched.items():
            if not last_ep:
                continue
            last_season = last_ep.get("season_number", 0)
            last_ep_num = last_ep.get("episode_number", 0)
            seasons = show_seasons_map.get(tid, [])
            # Sum completed seasons before the current airing season, plus episodes aired so far in it.
            aired_total = sum(
                s.get("episode_count", 0)
                for s in seasons
                if 0 < s.get("season_number", 0) < last_season
            ) + last_ep_num
            show_aired_count[tid] = aired_total
            collected = collected_map.get(tid, 0)
            show_pct[tid] = int((collected / aired_total) * 100) if aired_total > 0 else 0

        # Now we can accurately set watched_shows
        watched_shows = {
            tid for tid in show_tmdb_ids
            if show_watched_count_map.get(tid, 0) > 0
            and show_watched_count_map.get(tid, 0) >= show_aired_count.get(tid, 0)
        }

    # --- Collection state for movies/episodes ---
    collected_movie_ids: set[int] = set()
    if movie_tmdb_ids:
        coll_q = await db.execute(
            select(Media.tmdb_id)
            .join(Collection, Collection.media_id == Media.id)
            .where(Collection.user_id == user_id, Media.tmdb_id.in_(movie_tmdb_ids), Media.media_type == MediaType.movie)
            .distinct()
        )
        collected_movie_ids = {r[0] for r in coll_q.all()}

    collected_ep_ids: set[int] = set()
    if ep_tmdb_ids:
        coll_q = await db.execute(
            select(Media.tmdb_id)
            .join(Collection, Collection.media_id == Media.id)
            .where(Collection.user_id == user_id, Media.tmdb_id.in_(ep_tmdb_ids), Media.media_type == MediaType.episode)
            .distinct()
        )
        collected_ep_ids = {r[0] for r in coll_q.all()}

    # --- User ratings ---
    # Only fetch show/movie-level ratings (season_number IS NULL); season-specific ratings
    # are fetched separately in the show detail endpoints.
    user_ratings: dict[tuple, float] = {}
    if all_tmdb_ids:
        ratings_q = await db.execute(
            select(Media.tmdb_id, Media.media_type, func.max(Rating.rating))
            .join(Rating, Rating.media_id == Media.id)
            .where(
                Rating.user_id == user_id,
                Media.tmdb_id.in_(all_tmdb_ids),
                Rating.season_number.is_(None),
            )
            .group_by(Media.tmdb_id, Media.media_type)
        )
        for tmdb_id, media_type, rating_val in ratings_q.all():
            user_ratings[(tmdb_id, media_type.value)] = rating_val

    # --- Apply to items ---
    for item in items:
        tid = item.get("tmdb_id")
        t = item.get("type")
        if t == "movie":
            item["watched"] = tid in watched_movies
            in_lib = tid in collected_movie_ids
            item["in_library"] = in_lib
            item["collection_pct"] = 100 if in_lib else 0
        elif t == "series":
            item["watched"] = tid in watched_shows
            pct = show_pct.get(tid, 0)
            item["collection_pct"] = pct
            item["in_library"] = pct > 0
        elif t == "episode":
            item["watched"] = tid in watched_episodes
            in_lib = tid in collected_ep_ids
            item["in_library"] = in_lib
            item["collection_pct"] = 100 if in_lib else 0
        else:
            item["watched"] = False
            item["collection_pct"] = 0
            item["in_library"] = False

        item["in_lists"] = list_membership.get(tid, [])
        item["is_monitored"] = monitored_status.get(tid, False)
        item["request_enabled"] = request_enabled_map.get(tid, False)
        item["user_rating"] = user_ratings.get((tid, t))

    return items


async def get_user_tmdb_key(db: AsyncSession, user_id: int) -> str | None:
    result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    settings_row = result.scalar_one_or_none()
    return settings_row.tmdb_api_key if settings_row else None


def check_tmdb_key(api_key: str | None) -> bool:
    if api_key:
        return True
    return bool(getattr(tmdb.settings, "tmdb_api_key", None))


def _extract_movie_certification(data: dict, country: str = "US") -> str | None:
    for entry in data.get("release_dates", {}).get("results", []):
        if entry.get("iso_3166_1") == country:
            for rd in entry.get("release_dates", []):
                cert = rd.get("certification", "").strip()
                if cert:
                    return cert
    return None


def _extract_show_content_rating(data: dict, country: str = "US") -> str | None:
    for entry in data.get("content_ratings", {}).get("results", []):
        if entry.get("iso_3166_1") == country:
            rating = entry.get("rating", "").strip()
            if rating:
                return rating
    return None


def format_media(media: Media) -> dict:
    cast = []
    raw_cast = (media.tmdb_data or {}).get("cast", [])
    for c in raw_cast:
        cast.append(
            {
                "tmdb_id": c.get("id"),
                "name": c.get("name"),
                "character": c.get("character"),
                "profile_path": tmdb.poster_url(c.get("profile_path"))
                if c.get("profile_path")
                else None,
            }
        )

    return {
        "id": media.id,
        "tmdb_id": media.tmdb_id,
        "type": media.media_type,
        "title": media.title,
        "original_title": media.original_title,
        "overview": media.overview,
        "poster_path": media.poster_path,
        "backdrop_path": media.backdrop_path,
        "release_date": media.release_date,
        "runtime": media.runtime,
        "tmdb_rating": media.tmdb_rating,
        "tagline": media.tagline,
        "status": media.status,
        "season_number": media.season_number,
        "episode_number": media.episode_number,
        "show_title": media.show.title if media.show else None,
        "show_tmdb_id": media.show.tmdb_id if media.show else None,
        "show_poster_path": media.show.poster_path if media.show else None,
        "show_backdrop_path": media.show.backdrop_path if media.show else None,
        "genres": (media.tmdb_data or {}).get("genres", []),
        "cast": cast[:12],
        "collection": (media.tmdb_data or {}).get("collection"),
        "adult": media.adult,
    }


@router.get("")
async def list_media(
    type: MediaType | None = Query(None),
    sort: str = Query(default="created_at"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    genre: str | None = Query(None),
    year: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    offset = (page - 1) * page_size

    filters = [Collection.user_id == current_user.id]
    if type:
        filters.append(Media.media_type == type)
    if genre:
        filters.append(sa_cast(Media.tmdb_data["genres"], Text).contains(f'"{genre}"'))
    if year:
        filters.append(Media.release_date.like(f'{year}%'))

    base_query = (
        select(Media)
        .options(joinedload(Media.show))
        .join(Collection, Collection.media_id == Media.id)
        .where(*filters)
    )

    # Count total
    count_query = select(func.count()).select_from(base_query.subquery())
    total_result = await db.execute(count_query)
    total_count = total_result.scalar_one()
    total_pages = (total_count + page_size - 1) // page_size

    # Sort and Paginate
    sort_map = {
        "rating": Media.tmdb_rating.desc().nulls_last(),
        "release_date": Media.release_date.desc().nulls_last(),
        "title": func.lower(Media.title).asc(),
        "created_at": Collection.added_at.desc(),
    }
    order = sort_map.get(sort, Collection.added_at.desc())

    query = base_query.order_by(order).offset(offset).limit(page_size)
    result = await db.execute(query)
    items = result.scalars().all()

    results = [format_media(m) for m in items]
    await enrich_with_state(db, current_user.id, results)
    return {
        "page": page,
        "page_size": page_size,
        "total_results": total_count,
        "total_pages": total_pages,
        "results": results,
    }


@router.get("/search")
async def search_media(
    q: str = Query(..., min_length=2),
    type: str | None = Query(None),
    year: int | None = Query(None),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    valid_types = {m.value for m in MediaType} | {"person", "collection"}
    if type is not None and type not in valid_types:
        type = None

    # Collection search: TMDB only, no local DB
    if type == "collection":
        tmdb_key = await get_user_tmdb_key(db, current_user.id)
        if not check_tmdb_key(tmdb_key):
            return {"page": page, "total_pages": 1, "total_results": 0, "results": []}
        try:
            data = await tmdb.search_collection(q, page=page, api_key=tmdb_key)
            collections = [
                {
                    "id": None,
                    "tmdb_id": c.get("id"),
                    "type": "collection",
                    "title": c.get("name"),
                    "poster_path": tmdb.poster_url(c.get("poster_path")),
                    "backdrop_path": tmdb.poster_url(c.get("backdrop_path"), size="w1280"),
                    "overview": c.get("overview"),
                    "in_library": False,
                }
                for c in data.get("results", [])
            ]
        except Exception as e:
            print(f"TMDB collection search error: {e}")
            collections = []
            data = {}
        return {
            "page": page,
            "total_pages": data.get("total_pages", 1),
            "total_results": data.get("total_results", 0),
            "results": collections,
        }

    # People search: TMDB only, no local DB
    if type == "person":
        tmdb_key = await get_user_tmdb_key(db, current_user.id)
        if not check_tmdb_key(tmdb_key):
            return {"page": page, "total_pages": 1, "total_results": 0, "results": []}
        try:
            data = await tmdb.search_people(q, page=page, api_key=tmdb_key)
            people = [
                {
                    "id": None,
                    "tmdb_id": p.get("id"),
                    "type": "person",
                    "title": p.get("name"),
                    "poster_path": tmdb.poster_url(p.get("profile_path")),
                    "known_for_department": p.get("known_for_department"),
                    "in_library": False,
                }
                for p in data.get("results", [])
            ]
        except Exception as e:
            print(f"TMDB people search error: {e}")
            people = []
            data = {}
        return {
            "page": page,
            "total_pages": data.get("total_pages", 1),
            "total_results": data.get("total_results", 0),
            "results": people,
        }

    # Episode search: local DB only (TMDB has no episode search endpoint)
    if type == MediaType.episode:
        db_query = (
            select(Media)
            .options(joinedload(Media.show))
            .where(or_(Media.title.ilike(f"%{q}%"), Media.original_title.ilike(f"%{q}%")))
            .where(Media.media_type == MediaType.episode)
            .limit(50)
        )
        result = await db.execute(db_query)
        items = result.scalars().all()
        formatted = [format_media(m) for m in items]
        for item in formatted:
            item["in_library"] = True
        return {"page": 1, "total_pages": 1, "total_results": len(formatted), "results": formatted}

    tmdb_key = await get_user_tmdb_key(db, current_user.id)

    # No TMDB key: fall back to local title search
    if not check_tmdb_key(tmdb_key):
        db_query = (
            select(Media)
            .options(joinedload(Media.show))
            .where(or_(Media.title.ilike(f"%{q}%"), Media.original_title.ilike(f"%{q}%")))
            .limit(30)
        )
        if type:
            db_query = db_query.where(Media.media_type == type)
        else:
            db_query = db_query.where(Media.media_type != MediaType.episode)
        result = await db.execute(db_query)
        items = result.scalars().all()
        formatted = [format_media(m) for m in items]
        for item in formatted:
            item["in_library"] = True
        return {"page": 1, "total_pages": 1, "total_results": len(formatted), "results": formatted}

    # 1. Search TMDB (primary source for ordering)
    raw_results = []
    total_pages = 1
    total_results = 0
    try:
        if type == MediaType.movie:
            data = await tmdb.search_movies(q, page=page, year=year, api_key=tmdb_key)
            raw_results = data.get("results", [])
            for res in raw_results:
                res["media_type"] = "movie"
            total_pages = data.get("total_pages", 1)
            total_results = data.get("total_results", 0)
        elif type == MediaType.series:
            data = await tmdb.search_shows(q, page=page, year=year, api_key=tmdb_key)
            raw_results = data.get("results", [])
            for res in raw_results:
                res["media_type"] = "tv"
            total_pages = data.get("total_pages", 1)
            total_results = data.get("total_results", 0)
        else:
            # "All": movies + shows + people, interleaved by TMDB popularity score
            movie_data, show_data, people_data = await asyncio.gather(
                tmdb.search_movies(q, page=page, api_key=tmdb_key),
                tmdb.search_shows(q, page=page, api_key=tmdb_key),
                tmdb.search_people(q, page=page, api_key=tmdb_key),
            )
            movie_results = movie_data.get("results", [])
            for res in movie_results:
                res["media_type"] = "movie"
            show_results = show_data.get("results", [])
            for res in show_results:
                res["media_type"] = "tv"
            people_results = people_data.get("results", [])
            for res in people_results:
                res["media_type"] = "person"
            # Interleave by popularity so relevance is preserved across all three lists
            raw_results = sorted(
                movie_results + show_results + people_results,
                key=lambda x: x.get("popularity", 0),
                reverse=True,
            )
            total_pages = max(
                movie_data.get("total_pages", 1),
                show_data.get("total_pages", 1),
                people_data.get("total_pages", 1),
            )
            total_results = (
                movie_data.get("total_results", 0)
                + show_data.get("total_results", 0)
                + people_data.get("total_results", 0)
            )
    except Exception as e:
        print(f"TMDB search error: {e}")

    # 2. Check which TMDB results are in the local library.
    # Must filter by media_type: TMDB movie/show IDs are in separate namespaces but the
    # integers can collide with episode tmdb_ids in the local DB, corrupting the map.
    tmdb_ids_on_page = [res.get("id") for res in raw_results if res.get("id")]
    local_map: dict[tuple[int, str], Media] = {}
    if tmdb_ids_on_page:
        local_q = (
            select(Media)
            .options(joinedload(Media.show))
            .where(Media.tmdb_id.in_(tmdb_ids_on_page))
        )
        if type == MediaType.movie:
            local_q = local_q.where(Media.media_type == MediaType.movie)
        elif type == MediaType.series:
            local_q = local_q.where(Media.media_type == MediaType.series)
        else:
            # "All" search: only movies and series — episodes have their own separate tab
            local_q = local_q.where(Media.media_type.in_([MediaType.movie, MediaType.series]))
        local_result = await db.execute(local_q)
        local_map = {(m.tmdb_id, m.media_type.value): m for m in local_result.scalars().all()}

    # 3. Build enriched list preserving TMDB relevance order
    enriched = []
    seen_tmdb_ids = set()
    for res in raw_results:
        tmdb_id = res.get("id")
        media_type = res.get("media_type")
        if media_type == "tv":
            media_type = "series"
        if media_type not in ("movie", "series", "person"):
            continue

        seen_tmdb_ids.add(tmdb_id)

        if media_type == "person":
            enriched.append({
                "id": None,
                "tmdb_id": tmdb_id,
                "type": "person",
                "title": res.get("name"),
                "poster_path": tmdb.poster_url(res.get("profile_path")),
                "known_for_department": res.get("known_for_department"),
                "in_library": False,
            })
            continue

        local = local_map.get((tmdb_id, media_type))
        if local:
            item = format_media(local)
            item["type"] = media_type  # TMDB source of truth; local row may differ
            item["in_library"] = True
            # Fill in missing display fields from TMDB search result
            if not item.get("poster_path"):
                item["poster_path"] = tmdb.poster_url(res.get("poster_path"))
            if not item.get("release_date"):
                item["release_date"] = res.get("release_date") or res.get("first_air_date")
            if not item.get("title"):
                item["title"] = res.get("title") or res.get("name")
        else:
            item = {
                "id": None,
                "tmdb_id": tmdb_id,
                "type": media_type,
                "title": res.get("title") or res.get("name"),
                "original_title": res.get("original_title") or res.get("original_name"),
                "overview": res.get("overview"),
                "poster_path": tmdb.poster_url(res.get("poster_path")),
                "backdrop_path": tmdb.poster_url(res.get("backdrop_path"), size="w1280"),
                "release_date": res.get("release_date") or res.get("first_air_date"),
                "tmdb_rating": res.get("vote_average"),
                "in_library": False,
                "adult": res.get("adult", False),
            }
        enriched.append(item)

    # 4. On page 1, append local library items that TMDB didn't return
    if page == 1:
        fallback_q = (
            select(Media)
            .options(joinedload(Media.show))
            .where(or_(Media.title.ilike(f"%{q}%"), Media.original_title.ilike(f"%{q}%")))
            .where(Media.tmdb_id.notin_(seen_tmdb_ids))
            .limit(10)
        )
        if type:
            fallback_q = fallback_q.where(Media.media_type == type)
        else:
            fallback_q = fallback_q.where(Media.media_type != MediaType.episode)
        fallback_result = await db.execute(fallback_q)
        for m in fallback_result.scalars().all():
            item = format_media(m)
            item["in_library"] = True
            enriched.append(item)

    await enrich_with_state(db, current_user.id, enriched)
    return {
        "page": page,
        "total_pages": total_pages,
        "total_results": total_results,
        "results": enriched,
    }


async def _sync_trending(
    type: MediaType,
    page: int = 1,
    api_key: str | None = None,
):
    """Fetch trending data from TMDB."""
    if not check_tmdb_key(api_key):
        return {"results": [], "page": 1, "total_pages": 1, "total_results": 0}

    try:
        if type == MediaType.movie:
            data = await tmdb.get_trending_movies(page=page, api_key=api_key)
        else:
            data = await tmdb.get_trending_shows(page=page, api_key=api_key)
        return data
    except Exception as e:
        print(f"Error fetching trending from TMDB: {e}")
        return {"results": [], "page": 1, "total_pages": 1, "total_results": 0}


@router.get("/trending/movies")
async def trending_movies(
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    data = await _sync_trending(MediaType.movie, page, api_key=tmdb_key)
    tmdb_results = data.get("results", [])

    if not tmdb_results:
        return {"page": page, "total_pages": 1, "total_results": 0, "results": []}

    tmdb_ids = [res["id"] for res in tmdb_results]
    query = (
        select(Media)
        .options(joinedload(Media.show))
        .where(Media.tmdb_id.in_(tmdb_ids), Media.media_type == MediaType.movie)
    )
    result = await db.execute(query)
    local_map = {m.tmdb_id: m for m in result.scalars().all()}

    enriched = []
    for res in tmdb_results:
        tmdb_id = res["id"]
        if tmdb_id in local_map:
            enriched.append({**format_media(local_map[tmdb_id]), "in_library": True})
        else:
            enriched.append(
                {
                    "id": None,
                    "tmdb_id": tmdb_id,
                    "type": MediaType.movie,
                    "title": res.get("title"),
                    "poster_path": tmdb.poster_url(res.get("poster_path")),
                    "release_date": res.get("release_date"),
                    "tmdb_rating": res.get("vote_average"),
                    "in_library": False,
                    "adult": res.get("adult", False),
                }
            )
    await enrich_with_state(db, current_user.id, enriched)
    return {
        "page": data.get("page", 1),
        "total_pages": data.get("total_pages", 1),
        "total_results": data.get("total_results", 0),
        "results": enriched,
    }


@router.get("/trending/shows")
async def trending_shows(
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    data = await _sync_trending(MediaType.series, page, api_key=tmdb_key)
    tmdb_results = data.get("results", [])

    if not tmdb_results:
        return {"page": page, "total_pages": 1, "total_results": 0, "results": []}

    tmdb_ids = [res["id"] for res in tmdb_results]

    # Collect which of these show TMDB IDs the user has in their library
    collected_q = await db.execute(
        select(ShowModel.tmdb_id)
        .join(Media, Media.show_id == ShowModel.id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == current_user.id, ShowModel.tmdb_id.in_(tmdb_ids))
        .distinct()
    )
    in_library: set[int] = {row[0] for row in collected_q.all()}

    enriched = []
    for res in tmdb_results:
        tmdb_id = res["id"]
        enriched.append(
            {
                "id": None,
                "tmdb_id": tmdb_id,
                "type": MediaType.series,
                "title": res.get("name"),
                "poster_path": tmdb.poster_url(res.get("poster_path")),
                "backdrop_path": tmdb.poster_url(res.get("backdrop_path"), size="w1280"),
                "release_date": res.get("first_air_date"),
                "tmdb_rating": res.get("vote_average"),
                "in_library": tmdb_id in in_library,
                "adult": res.get("adult", False),
            }
        )
    await enrich_with_state(db, current_user.id, enriched)
    return {
        "page": data.get("page", 1),
        "total_pages": data.get("total_pages", 1),
        "total_results": data.get("total_results", 0),
        "results": enriched,
    }


@router.get("/on-air-today")
async def on_air_today(
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": [], "page": 1, "total_pages": 1, "total_results": 0}
    data = await tmdb.get_on_air_today(page=page, api_key=tmdb_key)
    results = [
        {
            "id": None,
            "tmdb_id": s.get("id"),
            "type": "series",
            "title": s.get("name"),
            "poster_path": tmdb.poster_url(s.get("poster_path")),
            "backdrop_path": tmdb.poster_url(s.get("backdrop_path"), size="w780"),
            "tmdb_rating": s.get("vote_average"),
            "release_date": s.get("first_air_date"),
        }
        for s in data.get("results", [])
    ]
    await enrich_with_state(db, current_user.id, results)
    return {
        "results": results,
        "page": data.get("page", page),
        "total_pages": data.get("total_pages", 1),
        "total_results": data.get("total_results", 0),
    }


@router.get("/airing-today/collected")
async def airing_today_collected(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Return shows airing today on TMDB that the user has in their collection."""
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}

    # Collect the user's show TMDB IDs in one query
    collected_q = await db.execute(
        select(ShowModel.tmdb_id)
        .join(Media, Media.show_id == ShowModel.id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == current_user.id)
        .distinct()
    )
    collected_tmdb_ids: set[int] = {row[0] for row in collected_q.all()}

    if not collected_tmdb_ids:
        return {"results": []}

    # Fetch page 1 to discover total_pages, then fetch remaining pages concurrently
    try:
        first = await tmdb.get_on_air_today(page=1, api_key=tmdb_key)
    except Exception as e:
        print(f"Error fetching airing-today from TMDB: {e}")
        return {"results": []}
    total_pages = min(first.get("total_pages", 1), 20)  # cap at 20 pages (400 shows)
    all_shows = list(first.get("results", []))

    if total_pages > 1:
        pages = await asyncio.gather(
            *[tmdb.get_on_air_today(page=p, api_key=tmdb_key) for p in range(2, total_pages + 1)],
            return_exceptions=True,
        )
        for page_data in pages:
            if isinstance(page_data, Exception):
                continue
            all_shows.extend(page_data.get("results", []))

    # Keep only shows in the user's collection
    collected_shows = [s for s in all_shows if s.get("id") in collected_tmdb_ids]

    if not collected_shows:
        return {"results": []}

    # Fetch show details in parallel to get today's episode (last/next_episode_to_air)
    from datetime import date
    today = date.today().isoformat()
    semaphore = asyncio.Semaphore(10)

    async def fetch_episode(show: dict) -> dict:
        async with semaphore:
            try:
                detail = await tmdb.get_show_light(show["id"], api_key=tmdb_key)
            except Exception:
                detail = {}

        episode: dict | None = None
        for candidate in (detail.get("last_episode_to_air"), detail.get("next_episode_to_air")):
            if candidate and candidate.get("air_date") == today:
                episode = candidate
                break

        show_name = show.get("name")
        if episode:
            return {
                "id": None,
                "tmdb_id": show["id"],
                "type": "episode",
                "title": episode.get("name") or show_name,
                "show_title": show_name,
                "show_tmdb_id": show["id"],
                "season_number": episode.get("season_number"),
                "episode_number": episode.get("episode_number"),
                "poster_path": tmdb.poster_url(episode.get("still_path"), size="w780")
                    or tmdb.poster_url(show.get("backdrop_path"), size="w780"),
                "backdrop_path": tmdb.poster_url(show.get("backdrop_path"), size="w780"),
                "tmdb_rating": show.get("vote_average"),
                "release_date": episode.get("air_date"),
                "adult": show.get("adult", False),
            }
        return {
            "id": None,
            "tmdb_id": show["id"],
            "type": "series",
            "title": show_name,
            "poster_path": tmdb.poster_url(show.get("poster_path")),
            "backdrop_path": tmdb.poster_url(show.get("backdrop_path"), size="w780"),
            "tmdb_rating": show.get("vote_average"),
            "release_date": show.get("first_air_date"),
            "adult": show.get("adult", False),
        }

    results = list(await asyncio.gather(*[fetch_episode(s) for s in collected_shows]))
    await enrich_with_state(db, current_user.id, results)
    return {"results": results}


@router.get("/recently-added")
async def recently_added(
    type: MediaType | None = Query(None),
    limit: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Subquery: latest added_at per media for this user (deduplicates movies in both Plex+Jellyfin)
    coll_subq = (
        select(Collection.media_id, func.max(Collection.added_at).label("max_added"))
        .where(Collection.user_id == current_user.id)
        .group_by(Collection.media_id)
        .subquery()
    )
    media_filters = [
        # Exclude episodes missing season/episode numbers — they are unidentifiable
        # orphans (created by webhook before the show was synced) and cannot be displayed.
        or_(
            Media.media_type != MediaType.episode,
            and_(Media.season_number.isnot(None), Media.episode_number.isnot(None)),
        )
    ]
    if type:
        media_filters.append(Media.media_type == type)
    query = (
        select(Media)
        .join(coll_subq, coll_subq.c.media_id == Media.id)
        .options(joinedload(Media.show))
        .where(*media_filters)
        .order_by(coll_subq.c.max_added.desc())
        .limit(limit)
    )
    result = await db.execute(query)
    items = [format_media(m) for m in result.scalars().all()]
    await enrich_with_state(db, current_user.id, items)
    return {"results": items}


@router.get("/person/{person_id}")
async def get_person_details(
    person_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        tmdb_key = await get_user_tmdb_key(db, current_user.id)
        if not check_tmdb_key(tmdb_key):
            raise HTTPException(status_code=404, detail="TMDB API Key not configured")
        data = await tmdb.get_person(person_id, api_key=tmdb_key)
        credits = data.get("combined_credits", {})
        cast_credits = credits.get("cast", [])
        formatted_credits = []
        for c in cast_credits:
            m_type = "movie" if c.get("media_type") == "movie" else "series"
            formatted_credits.append(
                {
                    "tmdb_id": c.get("id"),
                    "type": m_type,
                    "title": c.get("title") or c.get("name"),
                    "poster_path": tmdb.poster_url(c.get("poster_path")),
                    "release_date": c.get("release_date") or c.get("first_air_date"),
                    "character": c.get("character"),
                    "popularity": c.get("popularity", 0),
                    "adult": c.get("adult", False),
                }
            )
        formatted_credits.sort(key=lambda x: x["popularity"], reverse=True)
        top_credits = formatted_credits[:40]
        await enrich_with_state(db, current_user.id, top_credits)

        # Which of the user's lists contain this person?
        user_list_ids_q = await db.execute(select(UserList.id).where(UserList.user_id == current_user.id))
        user_list_ids = [r[0] for r in user_list_ids_q.all()]
        person_in_lists: list[int] = []
        if user_list_ids:
            li_q = await db.execute(
                select(ListItem.list_id)
                .join(Media, Media.id == ListItem.media_id)
                .where(
                    ListItem.list_id.in_(user_list_ids),
                    Media.tmdb_id == person_id,
                    Media.media_type == MediaType.person,
                )
            )
            person_in_lists = [r[0] for r in li_q.all()]

        return {
            "tmdb_id": data.get("id"),
            "name": data.get("name"),
            "biography": data.get("biography"),
            "profile_path": tmdb.poster_url(data.get("profile_path"), size="h632"),
            "birthday": data.get("birthday"),
            "place_of_birth": data.get("place_of_birth"),
            "known_for_department": data.get("known_for_department"),
            "credits": top_credits,
            "in_lists": person_in_lists,
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=404, detail=f"Person not found: {e}")


@router.get("/collection/{collection_id}")
async def get_collection_details(
    collection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        tmdb_key = await get_user_tmdb_key(db, current_user.id)
        if not check_tmdb_key(tmdb_key):
            raise HTTPException(status_code=404, detail="TMDB API Key not configured")

        data, genre_data = await asyncio.gather(
            tmdb.get_collection(collection_id, api_key=tmdb_key),
            tmdb.get_genre_list(api_key=tmdb_key),
        )

        genre_map = {g["id"]: g["name"] for g in genre_data.get("genres", [])}
        parts_data = sorted(data.get("parts", []), key=lambda x: x.get("release_date") or "")

        # Fetch credits for all parts in parallel (cap at 15 to avoid long waits)
        credit_results = await asyncio.gather(
            *[tmdb.get_movie_credits(p["id"], api_key=tmdb_key) for p in parts_data[:15]],
            return_exceptions=True,
        )

        # Aggregate unique genres from all parts
        all_genre_ids: set[int] = set()
        for p in parts_data:
            all_genre_ids.update(p.get("genre_ids", []))
        genres = [genre_map[gid] for gid in all_genre_ids if gid in genre_map]

        # Aggregate cast: rank by number of appearances across films, then popularity
        person_data: dict[int, dict] = {}
        for credits in credit_results:
            if isinstance(credits, Exception):
                continue
            for person in credits.get("cast", [])[:20]:
                pid = person.get("id")
                if pid not in person_data:
                    person_data[pid] = {
                        "tmdb_id": pid,
                        "name": person.get("name"),
                        "profile_path": tmdb.poster_url(person.get("profile_path"), size="w185"),
                        "appearances": 0,
                        "popularity": person.get("popularity", 0),
                    }
                person_data[pid]["appearances"] += 1

        cast = sorted(
            person_data.values(),
            key=lambda x: (-x["appearances"], -x["popularity"]),
        )[:15]

        parts = [
            {
                "tmdb_id": p.get("id"),
                "type": "movie",
                "title": p.get("title"),
                "poster_path": tmdb.poster_url(p.get("poster_path")),
                "backdrop_path": tmdb.poster_url(p.get("backdrop_path"), size="w1280"),
                "release_date": p.get("release_date"),
                "tmdb_rating": p.get("vote_average"),
                "overview": p.get("overview"),
            }
            for p in parts_data
        ]
        await enrich_with_state(db, current_user.id, parts)

        return {
            "id": data.get("id"),
            "name": data.get("name"),
            "overview": data.get("overview"),
            "poster_path": tmdb.poster_url(data.get("poster_path")),
            "backdrop_path": tmdb.poster_url(data.get("backdrop_path"), size="w1280"),
            "genres": genres,
            "cast": cast,
            "parts": parts,
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=404, detail=f"Collection not found: {e}")


@router.get("/tmdb/list")
async def get_tmdb_list(
    type: MediaType = Query(...),
    category: str = Query("popular"),
    page: int = Query(1, ge=1),
    genre: str | None = Query(None),
    year: int | None = Query(None),
    min_rating: float | None = Query(None),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        tmdb_key = await get_user_tmdb_key(db, current_user.id)
        if not check_tmdb_key(tmdb_key):
            return {"page": page, "total_pages": 1, "total_results": 0, "results": []}

        category_sort_map = {
            "popular": "popularity.desc",
            "top_rated": "vote_average.desc",
            "trending": "popularity.desc",
        }
        has_filters = bool(genre or year or min_rating or status)

        if has_filters:
            sort_by = category_sort_map.get(category, "popularity.desc")
            if type == MediaType.movie:
                genre_id = MOVIE_GENRE_IDS.get(genre) if genre else None
                data = await tmdb.discover_movies(
                    page=page, genre_id=genre_id, year=year,
                    min_rating=min_rating, sort_by=sort_by, api_key=tmdb_key,
                )
            else:
                genre_id = TV_GENRE_IDS.get(genre) if genre else None
                status_id = TV_STATUS_IDS.get(status) if status else None
                data = await tmdb.discover_shows(
                    page=page, genre_id=genre_id, year=year,
                    min_rating=min_rating, sort_by=sort_by,
                    status=status_id, api_key=tmdb_key,
                )
        elif type == MediaType.movie:
            if category == "top_rated":
                data = await tmdb.get_top_rated_movies(page=page, api_key=tmdb_key)
            elif category == "trending":
                data = await tmdb.get_trending_movies(page=page, api_key=tmdb_key)
            else:
                data = await tmdb.get_popular_movies(page=page, api_key=tmdb_key)
        else:  # series/episode
            if category == "top_rated":
                data = await tmdb.get_top_rated_shows(page=page, api_key=tmdb_key)
            elif category == "trending":
                data = await tmdb.get_trending_shows(page=page, api_key=tmdb_key)
            else:
                data = await tmdb.get_popular_shows(page=page, api_key=tmdb_key)

        results = data.get("results", [])
        tmdb_ids = [res["id"] for res in results]

        # Check local library
        if type == MediaType.series:
            # Match against Show.tmdb_id — never use episode tmdb_ids here,
            # as TMDB IDs across shows and episodes share the same number space
            # and collide (causing episodes to appear in show listings).
            show_q = (
                select(ShowModel.tmdb_id)
                .join(Media, Media.show_id == ShowModel.id)
                .join(Collection, Collection.media_id == Media.id)
                .where(
                    Collection.user_id == current_user.id,
                    ShowModel.tmdb_id.in_(tmdb_ids),
                )
                .distinct()
            )
            show_result = await db.execute(show_q)
            library_tmdb_ids = {row[0] for row in show_result.all()}
        else:
            query = (
                select(Media)
                .where(Media.tmdb_id.in_(tmdb_ids), Media.media_type == MediaType.movie)
            )
            result = await db.execute(query)
            library_tmdb_ids = {m.tmdb_id for m in result.scalars().all()}

        enriched = []
        for res in results:
            tmdb_id = res["id"]
            enriched.append(
                {
                    "id": None,
                    "tmdb_id": tmdb_id,
                    "type": type,
                    "title": res.get("title") or res.get("name"),
                    "poster_path": tmdb.poster_url(res.get("poster_path")),
                    "release_date": res.get("release_date") or res.get("first_air_date"),
                    "tmdb_rating": res.get("vote_average"),
                    "in_library": tmdb_id in library_tmdb_ids,
                    "adult": res.get("adult", False),
                }
            )
        await enrich_with_state(db, current_user.id, enriched)
        return {
            "page": data.get("page", 1),
            "total_pages": data.get("total_pages", 1),
            "total_results": data.get("total_results", 0),
            "results": enriched,
        }
    except Exception as e:
        print(f"Error fetching TMDB list: {e}")
        return {"page": page, "total_pages": 1, "total_results": 0, "results": []}


from sqlalchemy import delete as sa_delete
from pydantic import BaseModel as PydanticModel


class CollectRequest(PydanticModel):
    tmdb_id: int
    media_type: MediaType
    # Episode context — required when collecting an episode that doesn't exist in the DB yet
    series_tmdb_id: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None


class CollectSeasonRequest(PydanticModel):
    series_tmdb_id: int
    season_number: int


def _enrich_movie_list(results: list[dict], library_ids: set[int]) -> list[dict]:
    return [
        {
            "id": None,
            "tmdb_id": r["id"],
            "type": MediaType.movie,
            "title": r.get("title"),
            "poster_path": tmdb.poster_url(r.get("poster_path")),
            "backdrop_path": tmdb.poster_url(r.get("backdrop_path"), size="w1280"),
            "release_date": r.get("release_date"),
            "tmdb_rating": r.get("vote_average"),
            "in_library": r["id"] in library_ids,
            "adult": r.get("adult", False),
        }
        for r in results if r.get("id")
    ]


def _enrich_show_list(results: list[dict], library_ids: set[int]) -> list[dict]:
    return [
        {
            "id": None,
            "tmdb_id": r["id"],
            "type": MediaType.series,
            "title": r.get("name"),
            "poster_path": tmdb.poster_url(r.get("poster_path")),
            "backdrop_path": tmdb.poster_url(r.get("backdrop_path"), size="w1280"),
            "release_date": r.get("first_air_date"),
            "tmdb_rating": r.get("vote_average"),
            "in_library": r["id"] in library_ids,
            "adult": r.get("adult", False),
        }
        for r in results if r.get("id")
    ]


async def _movie_library_ids(db: AsyncSession, user_id: int, tmdb_ids: list[int]) -> set[int]:
    q = await db.execute(
        select(Media.tmdb_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == user_id, Media.tmdb_id.in_(tmdb_ids), Media.media_type == MediaType.movie)
        .distinct()
    )
    return {row[0] for row in q.all()}


async def _show_library_ids(db: AsyncSession, user_id: int, tmdb_ids: list[int]) -> set[int]:
    q = await db.execute(
        select(ShowModel.tmdb_id)
        .join(Media, Media.show_id == ShowModel.id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == user_id, ShowModel.tmdb_id.in_(tmdb_ids))
        .distinct()
    )
    return {row[0] for row in q.all()}


@router.get("/now-playing")
async def now_playing(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        data = await tmdb.get_now_playing(api_key=tmdb_key)
        results = data.get("results", [])
        ids = [r["id"] for r in results if r.get("id")]
        lib = await _movie_library_ids(db, current_user.id, ids)
        items = _enrich_movie_list(results, lib)
        await enrich_with_state(db, current_user.id, items)
        return {"results": items}
    except Exception:
        return {"results": []}


@router.get("/trending/trailers")
async def trending_trailers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        data = await tmdb.get_trending_movies(time_window="week", api_key=tmdb_key)
        movies = data.get("results", [])[:16]

        async def fetch_trailer(movie: dict) -> dict | None:
            try:
                vdata = await tmdb.get_movie_videos(movie["id"], api_key=tmdb_key)
                videos = vdata.get("results", [])
                trailer = next(
                    (v for v in videos if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("official")),
                    next((v for v in videos if v.get("site") == "YouTube" and v.get("type") == "Trailer"), None),
                )
                if not trailer:
                    return None
                return {
                    "tmdb_id": movie["id"],
                    "title": movie.get("title") or movie.get("name"),
                    "poster_path": tmdb.poster_url(movie.get("poster_path")),
                    "backdrop_path": tmdb.poster_url(movie.get("backdrop_path"), size="w780"),
                    "release_date": movie.get("release_date"),
                    "trailer_key": trailer["key"],
                    "trailer_name": trailer.get("name", ""),
                }
            except Exception:
                return None

        results_raw = await asyncio.gather(*[fetch_trailer(m) for m in movies])
        results = [r for r in results_raw if r is not None]
        return {"results": results}
    except Exception:
        return {"results": []}


@router.get("/upcoming")
async def upcoming_movies(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        data = await tmdb.get_upcoming_movies(api_key=tmdb_key)
        results = data.get("results", [])
        ids = [r["id"] for r in results if r.get("id")]
        lib = await _movie_library_ids(db, current_user.id, ids)
        items = _enrich_movie_list(results, lib)
        await enrich_with_state(db, current_user.id, items)
        return {"results": items}
    except Exception:
        return {"results": []}


@router.get("/on-air-this-week")
async def on_air_this_week(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        data = await tmdb.get_on_air_this_week(api_key=tmdb_key)
        results = data.get("results", [])
        ids = [r["id"] for r in results if r.get("id")]
        lib = await _show_library_ids(db, current_user.id, ids)
        items = _enrich_show_list(results, lib)
        await enrich_with_state(db, current_user.id, items)
        return {"results": items}
    except Exception:
        return {"results": []}


@router.get("/hidden-gems")
async def hidden_gems(
    type: MediaType = Query(MediaType.movie),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import random
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        page = random.randint(1, 5)
        if type == MediaType.movie:
            data = await tmdb.discover_movies(
                page=page, sort_by="vote_average.desc",
                min_rating=7.5, vote_count_min=150, vote_count_max=3000,
                api_key=tmdb_key,
            )
            results = data.get("results", [])
            ids = [r["id"] for r in results if r.get("id")]
            lib = await _movie_library_ids(db, current_user.id, ids)
            items = _enrich_movie_list(results, lib)
            await enrich_with_state(db, current_user.id, items)
            return {"results": items}
        else:
            data = await tmdb.discover_shows(
                page=page, sort_by="vote_average.desc",
                min_rating=7.5, vote_count_min=150, vote_count_max=3000,
                api_key=tmdb_key,
            )
            results = data.get("results", [])
            ids = [r["id"] for r in results if r.get("id")]
            lib = await _show_library_ids(db, current_user.id, ids)
            items = _enrich_show_list(results, lib)
            await enrich_with_state(db, current_user.id, items)
            return {"results": items}
    except Exception:
        return {"results": []}


@router.get("/top-rated-movies")
async def top_rated_movies(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        data = await tmdb.get_top_rated_movies(api_key=tmdb_key)
        results = data.get("results", [])
        ids = [r["id"] for r in results if r.get("id")]
        lib = await _movie_library_ids(db, current_user.id, ids)
        items = _enrich_movie_list(results, lib)
        await enrich_with_state(db, current_user.id, items)
        return {"results": items}
    except Exception:
        return {"results": []}


@router.get("/top-rated-shows")
async def top_rated_shows(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        data = await tmdb.get_top_rated_shows(api_key=tmdb_key)
        results = data.get("results", [])
        ids = [r["id"] for r in results if r.get("id")]
        lib = await _show_library_ids(db, current_user.id, ids)
        items = _enrich_show_list(results, lib)
        await enrich_with_state(db, current_user.id, items)
        return {"results": items}
    except Exception:
        return {"results": []}


# TMDB watch provider IDs for reference:
# Netflix=8, Amazon Prime=9, Apple TV+=350, Disney+=337, Max=1899, Hulu=15
@router.get("/for-you")
async def for_you(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import random
    from models.profile import UserProfileData

    cached = _FOR_YOU_CACHE.get(current_user.id)
    if cached and (_time.monotonic() - cached[0]) < _FOR_YOU_TTL:
        return cached[1]

    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}

    profile_q = await db.execute(
        select(UserProfileData).where(UserProfileData.user_id == current_user.id)
    )
    profile = profile_q.scalar_one_or_none()

    if not profile:
        return {"results": []}

    movie_genres = profile.movie_genres or []
    show_genres = profile.show_genres or []
    language: str | None = getattr(profile, "content_language", None)

    if not movie_genres and not show_genres:
        return {"results": []}

    selected_movie_genres = random.sample(movie_genres, min(2, len(movie_genres)))
    selected_show_genres = random.sample(show_genres, min(2, len(show_genres)))

    movie_coros = []
    show_coros = []

    for genre_name in selected_movie_genres:
        genre_id = MOVIE_GENRE_IDS.get(genre_name)
        if genre_id:
            movie_coros.append(tmdb.discover_movies(
                genre_id=genre_id,
                sort_by="popularity.desc",
                with_original_language=language,
                api_key=tmdb_key,
            ))

    for genre_name in selected_show_genres:
        genre_id = TV_GENRE_IDS.get(genre_name)
        if genre_id:
            show_coros.append(tmdb.discover_shows(
                genre_id=genre_id,
                sort_by="popularity.desc",
                with_original_language=language,
                api_key=tmdb_key,
            ))

    if not movie_coros and not show_coros:
        return {"results": []}

    all_results = await asyncio.gather(*(movie_coros + show_coros), return_exceptions=True)

    num_movie_coros = len(movie_coros)
    movie_raw: list[dict] = []
    show_raw: list[dict] = []

    for i, res in enumerate(all_results):
        if isinstance(res, Exception):
            continue
        raw = res.get("results", [])[:8]
        if i < num_movie_coros:
            movie_raw.extend(raw)
        else:
            show_raw.extend(raw)

    seen: set[int] = set()
    unique_movies: list[dict] = []
    for r in movie_raw:
        rid = r.get("id")
        if rid and rid not in seen:
            seen.add(rid)
            unique_movies.append(r)

    seen2: set[int] = set()
    unique_shows: list[dict] = []
    for r in show_raw:
        rid = r.get("id")
        if rid and rid not in seen2:
            seen2.add(rid)
            unique_shows.append(r)

    movie_ids = [r["id"] for r in unique_movies]
    show_ids = [r["id"] for r in unique_shows]

    movie_lib = await _movie_library_ids(db, current_user.id, movie_ids) if movie_ids else set()
    show_lib = await _show_library_ids(db, current_user.id, show_ids) if show_ids else set()

    movie_items = _enrich_movie_list(unique_movies, movie_lib)
    show_items = _enrich_show_list(unique_shows, show_lib)

    combined = movie_items + show_items
    random.shuffle(combined)

    await enrich_with_state(db, current_user.id, combined)
    result = {"results": combined[:20]}
    _FOR_YOU_CACHE[current_user.id] = (_time.monotonic(), result)
    return result


@router.get("/streaming")
async def streaming(
    provider_id: int,
    type: MediaType = Query(MediaType.movie),
    watch_region: str = Query("US"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        if type == MediaType.movie:
            data = await tmdb.discover_movies(
                watch_provider_id=provider_id,
                watch_region=watch_region,
                api_key=tmdb_key,
            )
            results = data.get("results", [])
            ids = [r["id"] for r in results if r.get("id")]
            lib = await _movie_library_ids(db, current_user.id, ids)
            items = _enrich_movie_list(results, lib)
        else:
            data = await tmdb.discover_shows(
                watch_provider_id=provider_id,
                watch_region=watch_region,
                api_key=tmdb_key,
            )
            results = data.get("results", [])
            ids = [r["id"] for r in results if r.get("id")]
            lib = await _show_library_ids(db, current_user.id, ids)
            items = _enrich_show_list(results, lib)
        await enrich_with_state(db, current_user.id, items)
        return {"results": items}
    except Exception:
        return {"results": []}


@router.get("/new-episodes")
async def new_episodes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}
    try:
        data = await tmdb.get_on_air_this_week(api_key=tmdb_key)
        results = data.get("results", [])
        ids = [r["id"] for r in results if r.get("id")]
        lib = await _show_library_ids(db, current_user.id, ids)
        items = _enrich_show_list(results, lib)
        await enrich_with_state(db, current_user.id, items)
        # Only return shows the user has in their library
        library_items = [i for i in items if i.get("in_library")]
        return {"results": library_items}
    except Exception:
        return {"results": []}



async def recommended(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import random
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}

    # Bulk-load all collected IDs for filtering later
    all_movie_ids_q = await db.execute(
        select(Media.tmdb_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == current_user.id, Media.media_type == MediaType.movie)
        .distinct()
    )
    all_collected_movie_ids: set[int] = {row[0] for row in all_movie_ids_q.all()}

    all_show_ids_q = await db.execute(
        select(ShowModel.tmdb_id)
        .join(Media, Media.show_id == ShowModel.id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == current_user.id)
        .distinct()
    )
    all_collected_show_ids: set[int] = {row[0] for row in all_show_ids_q.all()}

    if not all_collected_movie_ids and not all_collected_show_ids:
        return {"results": []}

    # Sample seed items from most recently added
    recent_movies_q = await db.execute(
        select(Media.tmdb_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == current_user.id, Media.media_type == MediaType.movie)
        .order_by(Collection.added_at.desc())
        .limit(10)
    )
    recent_movie_ids = [row[0] for row in recent_movies_q.all()]

    recent_shows_q = await db.execute(
        select(ShowModel.tmdb_id)
        .join(Media, Media.show_id == ShowModel.id)
        .join(Collection, Collection.media_id == Media.id)
        .where(Collection.user_id == current_user.id)
        .order_by(Collection.added_at.desc())
        .distinct()
        .limit(5)
    )
    recent_show_ids = [row[0] for row in recent_shows_q.all()]

    seed_movies = random.sample(recent_movie_ids, min(3, len(recent_movie_ids)))
    seed_shows = random.sample(recent_show_ids, min(2, len(recent_show_ids)))

    semaphore = asyncio.Semaphore(10)

    async def fetch_recs(tmdb_id: int, is_show: bool) -> list[dict]:
        async with semaphore:
            try:
                if is_show:
                    data = await tmdb.get_show_recommendations(tmdb_id, api_key=tmdb_key)
                else:
                    data = await tmdb.get_movie_recommendations(tmdb_id, api_key=tmdb_key)
                return data.get("results", [])
            except Exception:
                return []

    all_results = await asyncio.gather(
        *[fetch_recs(mid, False) for mid in seed_movies],
        *[fetch_recs(sid, True) for sid in seed_shows],
    )

    seen: set[int] = set()
    enriched: list[dict] = []
    n_movies = len(seed_movies)

    for i, batch in enumerate(all_results):
        is_show = i >= n_movies
        for item in batch:
            tmdb_id = item.get("id")
            if not tmdb_id or tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            if is_show and tmdb_id in all_collected_show_ids:
                continue
            if not is_show and tmdb_id in all_collected_movie_ids:
                continue
            if is_show:
                enriched.append({
                    "id": None,
                    "tmdb_id": tmdb_id,
                    "type": MediaType.series,
                    "title": item.get("name"),
                    "poster_path": tmdb.poster_url(item.get("poster_path")),
                    "release_date": item.get("first_air_date"),
                    "tmdb_rating": item.get("vote_average"),
                    "in_library": False,
                    "adult": item.get("adult", False),
                })
            else:
                enriched.append({
                    "id": None,
                    "tmdb_id": tmdb_id,
                    "type": MediaType.movie,
                    "title": item.get("title"),
                    "poster_path": tmdb.poster_url(item.get("poster_path")),
                    "release_date": item.get("release_date"),
                    "tmdb_rating": item.get("vote_average"),
                    "in_library": False,
                    "adult": item.get("adult", False),
                })

    random.shuffle(enriched)
    final = enriched[:20]
    await enrich_with_state(db, current_user.id, final)
    return {"results": final}


@router.post("/collect")
async def manually_collect(
    body: CollectRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually add a movie to the user's collection."""
    tmdb_key = await get_user_tmdb_key(db, current_user.id)

    # Find or create media record
    media_q = await db.execute(
        select(Media).where(Media.tmdb_id == body.tmdb_id, Media.media_type == body.media_type)
    )
    media = media_q.scalars().first()

    # If the episode row exists but is missing its show_id, link it now so season/show
    # collection percentages and season-page "collected" indicators stay consistent.
    if media and body.media_type == MediaType.episode and not media.show_id and body.series_tmdb_id:
        from models.show import Show as ShowModel
        show_link_q = await db.execute(select(ShowModel).where(ShowModel.tmdb_id == body.series_tmdb_id))
        show_link = show_link_q.scalar_one_or_none()
        if show_link:
            media.show_id = show_link.id

    if not media:
        if not check_tmdb_key(tmdb_key):
            raise HTTPException(status_code=404, detail="Media not found and no TMDB key configured")
        try:
            from core.enrichment import enrich_media
            if body.media_type == MediaType.movie:
                data = await tmdb.get_movie(body.tmdb_id, api_key=tmdb_key)
                title = data.get("title", "")
                media = Media(tmdb_id=body.tmdb_id, media_type=body.media_type, title=title)
                db.add(media)
                await db.flush()
                await enrich_media(media, api_key=tmdb_key)
            elif body.media_type == MediaType.episode:
                if not body.series_tmdb_id or body.season_number is None or body.episode_number is None:
                    raise HTTPException(
                        status_code=400,
                        detail="series_tmdb_id, season_number, and episode_number are required to collect a new episode",
                    )
                
                # Link to parent show
                from models.show import Show as ShowModel
                show_q = await db.execute(
                    select(ShowModel).where(ShowModel.tmdb_id == body.series_tmdb_id)
                )
                show = show_q.scalar_one_or_none()
                if not show:
                    # If show doesn't exist locally, create it first so the episode has a show_id
                    show_data = await tmdb.get_show(body.series_tmdb_id, api_key=tmdb_key)
                    show = ShowModel(
                        tmdb_id=body.series_tmdb_id,
                        title=show_data.get("name", ""),
                        poster_path=tmdb.poster_url(show_data.get("poster_path")),
                        backdrop_path=tmdb.poster_url(show_data.get("backdrop_path"), size="w1280"),
                        tmdb_rating=show_data.get("vote_average"),
                        status=show_data.get("status"),
                        first_air_date=show_data.get("first_air_date"),
                        last_air_date=show_data.get("last_air_date"),
                        tmdb_data={
                            "genres": [g["name"] for g in show_data.get("genres", [])],
                            "seasons": [
                                {
                                    "season_number": s["season_number"],
                                    "poster_path": tmdb.poster_url(s.get("poster_path")),
                                    "episode_count": s["episode_count"],
                                    "name": s["name"],
                                }
                                for s in show_data.get("seasons", [])
                            ]
                        }
                    )
                    db.add(show)
                    await db.flush()

                ep_data = await tmdb.get_episode(
                    body.series_tmdb_id, body.season_number, body.episode_number, api_key=tmdb_key
                )
                media = Media(
                    tmdb_id=body.tmdb_id,
                    media_type=MediaType.episode,
                    title=ep_data.get("name", ""),
                    season_number=body.season_number,
                    episode_number=body.episode_number,
                    show_id=show.id,
                )
                db.add(media)
                await db.flush()
                await enrich_media(media, api_key=tmdb_key, series_tmdb_id=body.series_tmdb_id)
            else:
                raise HTTPException(status_code=400, detail=f"Manual collection not supported for type: {body.media_type}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"TMDB lookup failed: {e}")

    # Check for existing collection entry
    existing_q = await db.execute(
        select(Collection).where(
            Collection.user_id == current_user.id,
            Collection.media_id == media.id,
        )
    )
    if existing_q.scalars().first():
        return {"status": "ok", "message": "Already in collection"}

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    coll_stmt = pg_insert(Collection).values(user_id=current_user.id, media_id=media.id)
    coll_stmt = coll_stmt.on_conflict_do_nothing(constraint="uq_collection_user_media")
    result = await db.execute(coll_stmt)
    await db.flush()
    coll_q = await db.execute(
        select(Collection).where(Collection.user_id == current_user.id, Collection.media_id == media.id)
    )
    coll = coll_q.scalar_one()
    db.add(CollectionFile(
        collection_id=coll.id,
        source=CollectionSource.manual,
        source_id=str(body.tmdb_id),
    ))
    await db.commit()
    return {"status": "ok", "message": "Added to collection"}


@router.delete("/collect")
async def manually_uncollect(
    tmdb_id: int | None = Query(None),
    media_id: int | None = Query(None, alias="id"),
    media_type: MediaType = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a manually-added item from the user's collection."""
    if not tmdb_id and not media_id:
        raise HTTPException(status_code=400, detail="Either tmdb_id or id is required")

    if tmdb_id:
        media_q = await db.execute(
            select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == media_type)
        )
    else:
        media_q = await db.execute(
            select(Media).where(Media.id == media_id, Media.media_type == media_type)
        )
    
    media = media_q.scalars().first()
    if not media:
        return {"status": "ok"}

    await db.execute(
        sa_delete(Collection).where(
            Collection.user_id == current_user.id,
            Collection.media_id == media.id,
        )
    )
    await db.commit()
    return {"status": "ok", "message": "Removed from collection"}


@router.post("/collect-season")
async def collect_season(
    body: CollectSeasonRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually add all episodes in a season to the user's collection."""
    from models.show import Show as ShowModel

    show_q = await db.execute(select(ShowModel).where(ShowModel.tmdb_id == body.series_tmdb_id))
    show = show_q.scalar_one_or_none()
    if not show:
        raise HTTPException(status_code=404, detail="Show not found in library")

    episodes_q = await db.execute(
        select(Media).where(
            Media.show_id == show.id,
            Media.media_type == MediaType.episode,
            Media.season_number == body.season_number,
        )
    )
    episodes = episodes_q.scalars().all()
    if not episodes:
        return {"status": "ok", "count": 0}

    added = 0
    for ep in episodes:
        existing_q = await db.execute(
            select(Collection).where(
                Collection.user_id == current_user.id,
                Collection.media_id == ep.id,
            )
        )
        if not existing_q.scalars().first():
            db.add(Collection(
                user_id=current_user.id,
                media_id=ep.id,
                source=CollectionSource.manual,
                source_id=str(ep.tmdb_id or ep.id),
            ))
            added += 1
    await db.commit()
    return {"status": "ok", "count": added}


@router.delete("/collect-season")
async def uncollect_season(
    series_tmdb_id: int = Query(...),
    season_number: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove all collection entries for all episodes in a season."""
    from models.show import Show as ShowModel

    show_q = await db.execute(select(ShowModel).where(ShowModel.tmdb_id == series_tmdb_id))
    show = show_q.scalar_one_or_none()
    if not show:
        return {"status": "ok"}

    episodes_q = await db.execute(
        select(Media.id).where(
            Media.show_id == show.id,
            Media.media_type == MediaType.episode,
            Media.season_number == season_number,
        )
    )
    episode_ids = [r[0] for r in episodes_q.all()]
    if not episode_ids:
        return {"status": "ok"}

    await db.execute(
        sa_delete(Collection).where(
            Collection.user_id == current_user.id,
            Collection.media_id.in_(episode_ids),
        )
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/{type}/{tmdb_id}/request")
async def request_media(
    type: MediaType,
    tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Request a movie (Radarr) or series (Sonarr)."""
    settings_q = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings = settings_q.scalar_one_or_none()
    
    if not settings:
        raise HTTPException(status_code=400, detail="User settings not configured")

    if type == MediaType.movie:
        if not all([settings.radarr_url, settings.radarr_token, settings.radarr_root_folder, settings.radarr_quality_profile]):
            raise HTTPException(status_code=400, detail="Radarr not configured in settings")
        
        from core import radarr
        try:
            # We need the title for the request pattern, or let radarr lookup do it
            res = await radarr.add_movie(
                url=settings.radarr_url,
                token=settings.radarr_token,
                tmdb_id=tmdb_id,
                title="", # Radarr client does a lookup by tmdb_id anyway
                root_folder=settings.radarr_root_folder,
                quality_profile_id=settings.radarr_quality_profile,
                tags=settings.radarr_tags
            )
            return res
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Radarr error: {e}")

    elif type == MediaType.series:
        if not all([settings.sonarr_url, settings.sonarr_token, settings.sonarr_root_folder, settings.sonarr_quality_profile]):
            raise HTTPException(status_code=400, detail="Sonarr not configured in settings")
        
        from core import sonarr, tmdb
        try:
            # Sonarr needs TVDB ID. TMDB /tv/{id}/external_ids provides it.
            tmdb_key = await get_user_tmdb_key(db, current_user.id)
            ext_ids = await tmdb.get_external_ids(tmdb_id, "tv", api_key=tmdb_key)
            tvdb_id = ext_ids.get("tvdb_id")
            
            if not tvdb_id:
                raise HTTPException(status_code=400, detail="Could not find TVDB ID for this show")

            res = await sonarr.add_series(
                url=settings.sonarr_url,
                token=settings.sonarr_token,
                tvdb_id=tvdb_id,
                root_folder=settings.sonarr_root_folder,
                quality_profile_id=settings.sonarr_quality_profile,
                tags=settings.sonarr_tags,
                season_folder=settings.sonarr_season_folder if settings.sonarr_season_folder is not None else True,
            )
            return res
        except Exception as e:
            if isinstance(e, HTTPException): raise e
            raise HTTPException(status_code=500, detail=f"Sonarr error: {e}")

    else:
        raise HTTPException(status_code=400, detail="Can only request movies or series")


async def refresh_technical_data(db: AsyncSession, media_ids: list[int], user_id: int) -> None:
    """For every CollectionFile the user has for the given media IDs, fetch fresh
    technical data (resolution, codecs, languages) from Plex, Jellyfin, or Emby.
    Manual entries are upgraded to the real source by searching all connections."""
    import core.plex as plex_client
    import core.jellyfin as jellyfin_client
    import core.emby as emby_client
    from models.show import Show as ShowModel

    # Load all connections for this user, grouped by type
    conns_result = await db.execute(
        select(MediaServerConnection).where(MediaServerConnection.user_id == user_id)
    )
    all_conns = conns_result.scalars().all()
    conns_by_id: dict[int, MediaServerConnection] = {c.id: c for c in all_conns}
    plex_conns    = [c for c in all_conns if c.type == "plex"]
    jellyfin_conns = [c for c in all_conns if c.type == "jellyfin"]
    emby_conns    = [c for c in all_conns if c.type == "emby"]

    if not all_conns:
        return

    files_result = await db.execute(
        select(CollectionFile, Collection, Media)
        .join(Collection, Collection.id == CollectionFile.collection_id)
        .join(Media, Media.id == Collection.media_id)
        .where(
            Collection.user_id == user_id,
            Collection.media_id.in_(media_ids),
        )
    )
    rows = files_result.all()
    if not rows:
        return

    # Pre-load shows for any episode rows
    show_ids = {media.show_id for _, _, media in rows if media.show_id is not None}
    show_tmdb_map: dict[int, int] = {}
    if show_ids:
        shows_result = await db.execute(select(ShowModel).where(ShowModel.id.in_(show_ids)))
        for s in shows_result.scalars().all():
            show_tmdb_map[s.id] = s.tmdb_id

    for cf, coll, media in rows:
        quality: dict = {}
        new_source: Optional[CollectionSource] = None
        new_source_id: Optional[str] = None
        new_connection_id: Optional[int] = None

        # Resolve the connection for this file (non-manual sources have connection_id set)
        conn = conns_by_id.get(cf.connection_id) if cf.connection_id else None

        if cf.source == CollectionSource.plex and conn and cf.source_id:
            item = await plex_client.get_item(conn.url, conn.token, cf.source_id)
            if item:
                quality = plex_client.extract_quality(item.get("Media", []))

        elif cf.source in (CollectionSource.jellyfin, CollectionSource.emby) and conn and cf.source_id:
            client_mod = jellyfin_client if cf.source == CollectionSource.jellyfin else emby_client
            item = await client_mod.get_item(conn.url, conn.token, cf.source_id, user_id=conn.server_user_id)
            if item:
                quality = client_mod.extract_quality(item.get("MediaStreams", []))
                if not quality.get("file_path") and item.get("Path"):
                    quality["file_path"] = item["Path"]

        elif cf.source == CollectionSource.manual and media.tmdb_id:
            # Try to find the item across all connections by TMDB metadata
            item = None
            if media.media_type == MediaType.movie:
                for c in plex_conns:
                    item = await plex_client.find_movie_by_tmdb_id(c.url, c.token, media.tmdb_id)
                    if item:
                        new_source = CollectionSource.plex
                        new_source_id = str(item.get("ratingKey", ""))
                        new_connection_id = c.id
                        quality = plex_client.extract_quality(item.get("Media", []))
                        break
                if not item:
                    for c in jellyfin_conns:
                        item = await jellyfin_client.find_movie_by_tmdb_id(c.url, c.token, media.tmdb_id)
                        if item:
                            new_source = CollectionSource.jellyfin
                            new_source_id = item.get("Id", "")
                            new_connection_id = c.id
                            quality = jellyfin_client.extract_quality(item.get("MediaStreams", []))
                            if not quality.get("file_path") and item.get("Path"):
                                quality["file_path"] = item["Path"]
                            break
                if not item:
                    for c in emby_conns:
                        item = await emby_client.find_movie_by_tmdb_id(c.url, c.token, media.tmdb_id)
                        if item:
                            new_source = CollectionSource.emby
                            new_source_id = item.get("Id", "")
                            new_connection_id = c.id
                            quality = emby_client.extract_quality(item.get("MediaStreams", []))
                            if not quality.get("file_path") and item.get("Path"):
                                quality["file_path"] = item["Path"]
                            break

            elif media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
                series_tmdb_id = show_tmdb_map.get(media.show_id) if media.show_id else None
                if series_tmdb_id:
                    for c in plex_conns:
                        item = await plex_client.find_episode_by_ids(
                            c.url, c.token, series_tmdb_id, media.season_number, media.episode_number,
                        )
                        if item:
                            new_source = CollectionSource.plex
                            new_source_id = str(item.get("ratingKey", ""))
                            new_connection_id = c.id
                            quality = plex_client.extract_quality(item.get("Media", []))
                            break
                    if not item:
                        for c in jellyfin_conns:
                            item = await jellyfin_client.find_episode_by_ids(
                                c.url, c.token, series_tmdb_id, media.season_number, media.episode_number,
                            )
                            if item:
                                new_source = CollectionSource.jellyfin
                                new_source_id = item.get("Id", "")
                                new_connection_id = c.id
                                quality = jellyfin_client.extract_quality(item.get("MediaStreams", []))
                                if not quality.get("file_path") and item.get("Path"):
                                    quality["file_path"] = item["Path"]
                                break
                    if not item:
                        for c in emby_conns:
                            item = await emby_client.find_episode_by_ids(
                                c.url, c.token, series_tmdb_id, media.season_number, media.episode_number,
                            )
                            if item:
                                new_source = CollectionSource.emby
                                new_source_id = item.get("Id", "")
                                new_connection_id = c.id
                                quality = emby_client.extract_quality(item.get("MediaStreams", []))
                                if not quality.get("file_path") and item.get("Path"):
                                    quality["file_path"] = item["Path"]
                                break

        if not quality.get("resolution"):
            continue

        # Upgrade manual entry to the real source so future syncs work
        if new_source and new_source_id:
            cf.source = new_source
            cf.source_id = new_source_id
        if new_connection_id:
            cf.connection_id = new_connection_id

        if quality.get("resolution"):    cf.resolution         = quality["resolution"]
        if quality.get("video_codec"):   cf.video_codec        = quality["video_codec"]
        if quality.get("audio_codec"):   cf.audio_codec        = quality["audio_codec"]
        if quality.get("audio_channels"): cf.audio_channels    = quality["audio_channels"]
        if quality.get("audio_languages") is not None: cf.audio_languages    = quality["audio_languages"]
        if quality.get("subtitle_languages") is not None: cf.subtitle_languages = quality["subtitle_languages"]
        if quality.get("file_path"):     cf.file_path          = quality["file_path"]


@router.post("/movie/{tmdb_id}/refresh")
async def refresh_movie_metadata(
    tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-fetch TMDB metadata for a movie the user has in their library."""
    from core.enrichment import enrich_media

    result = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.movie)
    )
    media = result.scalar_one_or_none()
    if not media:
        raise HTTPException(status_code=404, detail="Movie not found")

    coll_result = await db.execute(
        select(Collection).where(Collection.user_id == current_user.id, Collection.media_id == media.id)
    )
    if not coll_result.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Movie not in your library")

    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    await enrich_media(media, api_key=tmdb_key)

    await refresh_technical_data(db, [media.id], current_user.id)

    await db.commit()
    return {"message": "Metadata refreshed successfully"}


async def get_where_to_watch(
    db: AsyncSession,
    user_id: int,
    tmdb_id: int,
    media_type: MediaType,
    media: "Media | None" = None,
    show: "ShowModel | None" = None,
    tmdb_key: str | None = None,
) -> list[dict]:
    """Return a deduplicated list of local servers and streaming services where this title is available."""
    sources: list[dict] = []
    seen_names: set[str] = set()

    def _add(entry: dict) -> None:
        key = (entry["type"], entry["name"])
        if key not in seen_names:
            seen_names.add(key)
            sources.append(entry)

    # ── Local media servers ───────────────────────────────────────────────────
    if media_type == MediaType.movie and media:
        files_q = await db.execute(
            select(CollectionFile, MediaServerConnection)
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .outerjoin(MediaServerConnection, MediaServerConnection.id == CollectionFile.connection_id)
            .where(Collection.media_id == media.id, Collection.user_id == user_id)
        )
        for cf, conn in files_q.all():
            name = conn.name if conn else cf.source.value.title()
            _add({"type": cf.source.value, "name": name, "logo": None})

    elif media_type == MediaType.series and show:
        files_q = await db.execute(
            select(CollectionFile.connection_id, CollectionFile.source, MediaServerConnection.name)
            .distinct()
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .join(Media, Media.id == Collection.media_id)
            .outerjoin(MediaServerConnection, MediaServerConnection.id == CollectionFile.connection_id)
            .where(
                Media.show_id == show.id,
                Collection.user_id == user_id,
                Media.media_type == MediaType.episode,
            )
        )
        for _cid, src, conn_name in files_q.all():
            name = conn_name if conn_name else src.value.title()
            _add({"type": src.value, "name": name, "logo": None})

    # ── TMDB streaming providers ──────────────────────────────────────────────
    if tmdb_key and check_tmdb_key(tmdb_key):
        try:
            profile_q = await db.execute(
                select(UserProfileData).where(UserProfileData.user_id == user_id)
            )
            profile = profile_q.scalar_one_or_none()
            country = (profile.country if profile and profile.country else None) or "US"
            user_streaming_ids = (
                {int(s) for s in (profile.streaming_services or [])} if profile else set()
            )

            if media_type == MediaType.movie:
                providers_data = await tmdb.get_movie_watch_providers(tmdb_id, api_key=tmdb_key)
            else:
                providers_data = await tmdb.get_show_watch_providers(tmdb_id, api_key=tmdb_key)

            country_data = (providers_data.get("results") or {}).get(country, {})
            for p in country_data.get("flatrate", []):
                pid = p.get("provider_id")
                if user_streaming_ids and pid not in user_streaming_ids:
                    continue
                logo = tmdb.poster_url(p.get("logo_path"), size="w92") if p.get("logo_path") else None
                _add({"type": "streaming", "name": p.get("provider_name"), "logo": logo})
        except Exception:
            pass

    return sources


def _srt_to_vtt(srt: str) -> str:
    vtt = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", srt)
    return "WEBVTT\n\n" + vtt.strip()


@router.get("/playback/{type}/{tmdb_id}")
async def get_playback_sources(
    type: MediaType,
    tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return available local-server playback sources for a movie or episode."""
    if type not in (MediaType.movie, MediaType.episode):
        raise HTTPException(400, "Only movie/episode streaming supported")

    media_q = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == type)
    )
    media = media_q.scalars().first()
    if not media:
        return []

    files_q = await db.execute(
        select(CollectionFile, MediaServerConnection)
        .join(Collection, Collection.id == CollectionFile.collection_id)
        .outerjoin(MediaServerConnection, MediaServerConnection.id == CollectionFile.connection_id)
        .where(
            Collection.media_id == media.id,
            Collection.user_id == current_user.id,
            CollectionFile.source.in_([CollectionSource.jellyfin, CollectionSource.emby, CollectionSource.plex]),
            CollectionFile.connection_id.isnot(None),
            CollectionFile.source_id.isnot(None),
        )
    )

    from core import jellyfin as jellyfin_core
    from core import plex as plex_core

    sources = []
    for cf, conn in files_q.all():
        if not conn:
            continue
        resolution = cf.resolution
        subtitles: list[dict] = []
        audio_tracks: list[dict] = []

        if cf.source.value in ("jellyfin", "emby") and cf.source_id:
            try:
                item = await jellyfin_core.get_item(conn.url, conn.token, cf.source_id, user_id=conn.server_user_id)
                if item:
                    if resolution is None:
                        q = jellyfin_core.extract_quality(item.get("MediaStreams", []))
                        resolution = q.get("resolution")
                    for stream in item.get("MediaStreams", []):
                        if stream.get("Type") == "Subtitle":
                            codec = (stream.get("Codec") or "").lower()
                            # Skip image-based subtitle formats — they cannot be served as VTT
                            if codec in {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvbsub", "dvb_subtitle"}:
                                continue
                            lang = stream.get("Language") or None
                            label = stream.get("DisplayTitle") or stream.get("Title") or lang or "Subtitle"
                            subtitles.append({
                                "index": stream.get("Index"),
                                "language": lang,
                                "label": label,
                                "codec": codec,
                            })
                        elif stream.get("Type") == "Audio":
                            lang = stream.get("Language") or None
                            label = stream.get("DisplayTitle") or stream.get("Title") or lang or "Audio"
                            audio_tracks.append({
                                "index": stream.get("Index"),
                                "language": lang,
                                "label": label,
                                "codec": stream.get("Codec"),
                            })
            except Exception:
                pass

        elif cf.source.value == "plex" and cf.source_id:
            _plex_image_codecs = {"pgssub", "vobsub", "dvd_subtitle", "dvbsub"}
            try:
                item = await plex_core.get_item(conn.url, conn.token, cf.source_id)
                if item:
                    media_list = item.get("Media", [])
                    if media_list and media_list[0].get("Part"):
                        for stream in media_list[0]["Part"][0].get("Stream", []):
                            stype = stream.get("streamType")
                            if stype == 3 and stream.get("key"):
                                codec = (stream.get("codec") or "").lower()
                                if codec in _plex_image_codecs:
                                    continue
                                lang = stream.get("languageCode") or stream.get("languageTag") or None
                                label = stream.get("displayTitle") or stream.get("title") or lang or "Subtitle"
                                subtitles.append({
                                    "index": stream.get("id"),
                                    "language": lang,
                                    "label": label,
                                    "codec": codec,
                                })
                            elif stype == 2:
                                lang = stream.get("languageCode") or stream.get("languageTag") or None
                                label = stream.get("displayTitle") or stream.get("title") or lang or "Audio"
                                audio_tracks.append({
                                    "index": stream.get("id"),
                                    "language": lang,
                                    "label": label,
                                    "codec": stream.get("codec"),
                                })
            except Exception:
                pass

        sources.append({
            "connection_id": cf.connection_id,
            "source": cf.source.value,
            "name": conn.name or cf.source.value.title(),
            "resolution": resolution,
            "subtitles": subtitles,
            "audio_tracks": audio_tracks,
        })

    return sources


@router.get("/subtitles/{type}/{tmdb_id}")
async def get_subtitle(
    type: MediaType,
    tmdb_id: int,
    connection_id: int,
    stream_index: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy a subtitle track as WebVTT from a Jellyfin, Emby, or Plex server."""
    if type not in (MediaType.movie, MediaType.episode):
        raise HTTPException(400, "Only movie/episode subtitles supported")

    media_q = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == type)
    )
    media = media_q.scalars().first()
    if not media:
        raise HTTPException(404, "Not in library")

    cf_q = await db.execute(
        select(CollectionFile, MediaServerConnection)
        .join(Collection, Collection.id == CollectionFile.collection_id)
        .outerjoin(MediaServerConnection, MediaServerConnection.id == CollectionFile.connection_id)
        .where(
            Collection.media_id == media.id,
            Collection.user_id == current_user.id,
            CollectionFile.connection_id == connection_id,
        )
    )
    row = cf_q.first()
    if not row:
        raise HTTPException(404, "Source not found")

    cf, conn = row
    if not conn or not cf.source_id:
        raise HTTPException(400, "No valid connection configured")

    cache_headers = {"Cache-Control": "private, max-age=3600"}

    if cf.source.value in ("jellyfin", "emby"):
        sub_url = (
            f"{conn.url.rstrip('/')}/Videos/{cf.source_id}"
            f"/{cf.source_id}/Subtitles/{stream_index}/0/Stream.vtt"
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            res = await client.get(sub_url, headers={"X-Emby-Token": conn.token})
        if res.status_code >= 400:
            raise HTTPException(502, "Subtitle not available")
        return Response(content=res.content, media_type="text/vtt", headers=cache_headers)

    elif cf.source.value == "plex":
        from core import plex as plex_core
        item = await plex_core.get_item(conn.url, conn.token, cf.source_id)
        if not item:
            raise HTTPException(502, "Could not fetch item from Plex")
        sub_key = None
        sub_codec = ""
        media_list = item.get("Media", [])
        if media_list and media_list[0].get("Part"):
            for stream in media_list[0]["Part"][0].get("Stream", []):
                if stream.get("streamType") == 3 and stream.get("id") == stream_index:
                    sub_key = stream.get("key")
                    sub_codec = (stream.get("codec") or "").lower()
                    break
        if not sub_key:
            raise HTTPException(404, "Subtitle track not found")
        sub_url = f"{conn.url.rstrip('/')}{sub_key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            res = await client.get(sub_url, headers={"X-Plex-Token": conn.token})
        if res.status_code >= 400:
            raise HTTPException(502, "Subtitle not available from Plex")
        content = res.text
        if sub_codec in ("subrip", "srt") or not content.strip().startswith("WEBVTT"):
            content = _srt_to_vtt(content)
        return Response(content=content.encode("utf-8"), media_type="text/vtt", headers=cache_headers)

    else:
        raise HTTPException(400, f"Subtitles not supported for source: {cf.source.value}")


@router.get("/stream/{type}/{tmdb_id}")
async def stream_media(
    type: MediaType,
    tmdb_id: int,
    connection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy a direct video stream from a Plex server (Jellyfin/Emby use the HLS endpoint)."""
    if type not in (MediaType.movie, MediaType.episode):
        raise HTTPException(400, "Only movie/episode streaming supported")

    media_q = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == type)
    )
    media = media_q.scalars().first()
    if not media:
        raise HTTPException(404, "Not in library")

    cf_q = await db.execute(
        select(CollectionFile, MediaServerConnection)
        .join(Collection, Collection.id == CollectionFile.collection_id)
        .outerjoin(MediaServerConnection, MediaServerConnection.id == CollectionFile.connection_id)
        .where(
            Collection.media_id == media.id,
            Collection.user_id == current_user.id,
            CollectionFile.connection_id == connection_id,
        )
    )
    row = cf_q.first()
    if not row:
        raise HTTPException(404, "Source not found")

    cf, conn = row
    if not conn or not cf.source_id:
        raise HTTPException(400, "No valid connection configured")

    upstream_headers: dict[str, str] = {}
    range_header = request.headers.get("Range")
    if range_header:
        upstream_headers["Range"] = range_header

    if cf.source.value in ("jellyfin", "emby"):
        upstream_headers["X-Emby-Token"] = conn.token
        stream_url = f"{conn.url.rstrip('/')}/Videos/{cf.source_id}/stream"
        params: dict = {"Static": "true"}
    elif cf.source.value == "plex":
        from core import plex as plex_core
        item = await plex_core.get_item(conn.url, conn.token, cf.source_id)
        if not item:
            raise HTTPException(502, "Could not fetch item from Plex")
        media_list = item.get("Media", [])
        if not media_list or not media_list[0].get("Part"):
            raise HTTPException(502, "No media part found in Plex")
        part_key = media_list[0]["Part"][0]["key"]
        stream_url = f"{conn.url.rstrip('/')}{part_key}"
        upstream_headers["X-Plex-Token"] = conn.token
        params = {}
    else:
        raise HTTPException(400, f"Streaming not supported for source: {cf.source.value}")

    try:
        client = httpx.AsyncClient(timeout=httpx.Timeout(None))
        upstream_req = client.build_request("GET", stream_url, headers=upstream_headers, params=params)
        upstream_res = await client.send(upstream_req, stream=True)
    except Exception as e:
        raise HTTPException(502, f"Could not connect to media server: {e}")

    res_headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    for h in ("Content-Type", "Content-Length", "Content-Range"):
        v = upstream_res.headers.get(h.lower())
        if v:
            res_headers[h] = v

    async def cleanup() -> None:
        await upstream_res.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream_res.aiter_bytes(65536),
        status_code=upstream_res.status_code,
        headers=res_headers,
        background=BackgroundTask(cleanup),
    )


def _rewrite_m3u8_urls(content: str, connection_id: int, request_path: str, inherit_qs: str = "") -> str:
    """Rewrite every URL line in an M3U8 manifest to route through our HLS segment proxy.

    inherit_qs: raw query string from the parent manifest URL (e.g. "DeviceId=scrob&PlaySessionId=...&api_key=...").
    Jellyfin variant playlists use bare relative paths like "0.ts" with no query params; Jellyfin
    still needs DeviceId and PlaySessionId on those requests to locate the active transcoding job.
    Inheriting the parent query string restores them.
    """
    base_dir = request_path.rsplit("/", 1)[0] if "/" in request_path else ""
    lines = content.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        # Resolve absolute vs relative path
        if stripped.startswith("http://") or stripped.startswith("https://"):
            parsed = urllib.parse.urlparse(stripped)
            path_qs = parsed.path + ("?" + parsed.query if parsed.query else "")
        elif stripped.startswith("/"):
            path_qs = stripped
        else:
            # Relative — resolve against the directory of the current manifest and
            # inherit the parent manifest's session query params (DeviceId, PlaySessionId, api_key).
            path_qs = base_dir + "/" + stripped
            if inherit_qs and "?" not in path_qs:
                path_qs += "?" + inherit_qs
        encoded = urllib.parse.quote(path_qs, safe="")
        out.append(f"/api/proxy/media/hls-segment?connection_id={connection_id}&path={encoded}")
    return "\n".join(out)


async def _get_conn_for_user(connection_id: int, user_id: int, db: AsyncSession) -> MediaServerConnection:
    """Fetch a MediaServerConnection and verify it belongs to the given user."""
    result = await db.execute(
        select(MediaServerConnection).where(
            MediaServerConnection.id == connection_id,
            MediaServerConnection.user_id == user_id,
        )
    )
    conn = result.scalars().first()
    if not conn:
        raise HTTPException(404, "Connection not found")
    return conn


@router.get("/hls/{type}/{tmdb_id}")
async def hls_master_manifest(
    type: MediaType,
    tmdb_id: int,
    connection_id: int,
    audio_stream_index: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch and rewrite an HLS master M3U8 from Emby/Jellyfin so all segment URLs
    are proxied through this server. Creates a proper transcoding session on the server."""
    if type not in (MediaType.movie, MediaType.episode):
        raise HTTPException(400, "Only movie/episode HLS supported")

    media_q = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == type)
    )
    media = media_q.scalars().first()
    if not media:
        raise HTTPException(404, "Not in library")

    cf_q = await db.execute(
        select(CollectionFile, MediaServerConnection)
        .join(Collection, Collection.id == CollectionFile.collection_id)
        .outerjoin(MediaServerConnection, MediaServerConnection.id == CollectionFile.connection_id)
        .where(
            Collection.media_id == media.id,
            Collection.user_id == current_user.id,
            CollectionFile.connection_id == connection_id,
        )
    )
    row = cf_q.first()
    if not row:
        raise HTTPException(404, "Source not found")

    cf, conn = row
    if not conn or not cf.source_id:
        raise HTTPException(400, "No valid connection configured")
    if cf.source.value not in ("jellyfin", "emby"):
        raise HTTPException(400, "HLS streaming is only supported for Jellyfin/Emby sources")

    manifest_path = f"/Videos/{cf.source_id}/master.m3u8"
    manifest_url = f"{conn.url.rstrip('/')}{manifest_path}"

    # Generate a PlaySessionId so Emby/Jellyfin can track the transcoding session.
    # Emby requires POST /PlaybackInfo with this ID to initialise the session before
    # the first segment is requested; without it Emby throws "Value cannot be null (key)".
    play_session_id = uuid.uuid4().hex
    tag = ""
    media_source_id = cf.source_id
    try:
        info_url = f"{conn.url.rstrip('/')}/Items/{cf.source_id}/PlaybackInfo"
        common_headers = {"X-Emby-Token": conn.token, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10) as info_client:
            if conn.type == "emby":
                # Emby requires POST to initialise the transcoding session and bind PlaySessionId
                info_body: dict = {
                    "DeviceId": "scrob",
                    "PlaySessionId": play_session_id,
                    "MaxStreamingBitrate": 140_000_000,
                }
                if conn.server_user_id:
                    info_body["UserId"] = conn.server_user_id
                info_res = await info_client.post(info_url, json=info_body, headers=common_headers)
            else:
                # Jellyfin accepts GET; POST also works but GET is simpler
                info_params: dict = {}
                if conn.server_user_id:
                    info_params["UserId"] = conn.server_user_id
                info_res = await info_client.get(info_url, params=info_params, headers=common_headers)
        info_data = info_res.json()
        # Server may echo back or generate a session ID — use whichever is set
        play_session_id = info_data.get("PlaySessionId") or play_session_id
        media_sources = info_data.get("MediaSources", [])
        source_meta = next((s for s in media_sources if s.get("Id") == cf.source_id), None)
        if source_meta is None and media_sources:
            source_meta = media_sources[0]
        if source_meta:
            tag = source_meta.get("ETag", "")
            media_source_id = source_meta.get("Id", cf.source_id)
    except Exception:
        pass  # proceed without Tag; non-Emby servers may not require it

    params: dict[str, str] = {
        "VideoCodec": "copy",
        "MediaSourceId": media_source_id,
        "DeviceId": "scrob",
        "PlaySessionId": play_session_id,
        "api_key": conn.token,
    }
    if audio_stream_index is not None:
        params["AudioStreamIndex"] = str(audio_stream_index)
    if tag:
        params["Tag"] = tag
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(
                manifest_url,
                params=params,
                headers={"X-Emby-Token": conn.token},
            )
        if res.status_code != 200:
            raise HTTPException(502, f"Media server returned {res.status_code} for HLS manifest — body: {res.text[:300]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not fetch HLS manifest: {e}")

    rewritten = _rewrite_m3u8_urls(res.text, connection_id, manifest_path)
    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/hls-segment")
async def hls_segment_proxy(
    connection_id: int,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy HLS sub-manifests and TS segments from Emby/Jellyfin.
    For M3U8 responses, rewrites embedded URLs before returning."""
    conn = await _get_conn_for_user(connection_id, current_user.id, db)

    # path is the decoded absolute path (+ query string) on the media server.
    # Emby/Jellyfin HLS sub-playlists often use relative segment paths with no auth
    # query params. Always inject api_key so the session lookup on the server succeeds.
    segment_url = f"{conn.url.rstrip('/')}{path}"
    if conn.type in ("jellyfin", "emby") and "api_key=" not in path:
        sep = "&" if "?" in path else "?"
        segment_url += f"{sep}api_key={conn.token}"
    try:
        client = httpx.AsyncClient(timeout=httpx.Timeout(None))
        upstream_req = client.build_request(
            "GET", segment_url, headers={"X-Emby-Token": conn.token}
        )
        upstream_res = await client.send(upstream_req, stream=True)
    except Exception as e:
        raise HTTPException(502, f"Could not fetch HLS segment: {e}")

    content_type = upstream_res.headers.get("content-type", "")
    is_manifest = "mpegurl" in content_type or path.split("?")[0].endswith(".m3u8")

    if is_manifest:
        # Read the full body, rewrite URLs, return as text
        body = await upstream_res.aread()
        await upstream_res.aclose()
        await client.aclose()
        # Strip query string for path resolution, but pass it as inherit_qs so that
        # bare relative segment paths (e.g. "0.ts") get DeviceId/PlaySessionId/api_key appended.
        base_path = path.split("?")[0]
        inherit_qs = path.split("?", 1)[1] if "?" in path else ""
        rewritten = _rewrite_m3u8_urls(body.decode("utf-8"), connection_id, base_path, inherit_qs)
        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    # Binary segment (TS/fMP4) — stream through
    res_headers: dict[str, str] = {}
    for h in ("Content-Type", "Content-Length"):
        v = upstream_res.headers.get(h.lower())
        if v:
            res_headers[h] = v

    async def cleanup() -> None:
        await upstream_res.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream_res.aiter_bytes(65536),
        status_code=upstream_res.status_code,
        headers=res_headers,
        background=BackgroundTask(cleanup),
    )


@router.post("/session/report/{type}/{tmdb_id}")
async def report_session(
    type: MediaType,
    tmdb_id: int,
    body: SessionReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Report playback state to the media server so the session appears in its Now Playing dashboard."""
    if type not in (MediaType.movie, MediaType.episode):
        return {"ok": False}

    media_q = await db.execute(
        select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == type)
    )
    media = media_q.scalars().first()
    if not media:
        return {"ok": False}

    cf_q = await db.execute(
        select(CollectionFile, MediaServerConnection)
        .join(Collection, Collection.id == CollectionFile.collection_id)
        .outerjoin(MediaServerConnection, MediaServerConnection.id == CollectionFile.connection_id)
        .where(
            Collection.media_id == media.id,
            Collection.user_id == current_user.id,
            CollectionFile.connection_id == body.connection_id,
        )
    )
    row = cf_q.first()
    if not row:
        return {"ok": False}

    cf, conn = row
    if not conn or not cf.source_id:
        return {"ok": False}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            if cf.source.value in ("jellyfin", "emby"):
                pos_ticks = body.position_ms * 10_000  # Jellyfin uses 100-nanosecond ticks
                # X-Emby-Authorization is required for Jellyfin/Emby to associate the
                # playback report with a client session and show it in "Now Playing".
                device_id = f"scrob-{current_user.id}"
                auth_header = (
                    f'MediaBrowser Token="{conn.token}", Device="Scrob",'
                    f' DeviceId="{device_id}", Version="1.0.0"'
                )
                headers = {
                    "X-Emby-Token": conn.token,
                    "X-Emby-Authorization": auth_header,
                    "Content-Type": "application/json",
                }
                base = conn.url.rstrip("/")
                if body.state == "playing":
                    await client.post(
                        f"{base}/Sessions/Playing",
                        json={
                            "ItemId": cf.source_id,
                            "PositionTicks": pos_ticks,
                            "CanSeek": True,
                            "IsPaused": False,
                            "IsMuted": False,
                            "PlayMethod": "DirectPlay",
                        },
                        headers=headers,
                    )
                elif body.state in ("progress", "paused"):
                    await client.post(
                        f"{base}/Sessions/Playing/Progress",
                        json={
                            "ItemId": cf.source_id,
                            "PositionTicks": pos_ticks,
                            "IsPaused": body.state == "paused",
                            "IsMuted": False,
                        },
                        headers=headers,
                    )
                elif body.state == "stopped":
                    await client.post(
                        f"{base}/Sessions/Playing/Stopped",
                        json={"ItemId": cf.source_id, "PositionTicks": pos_ticks},
                        headers=headers,
                    )
            elif cf.source.value == "plex":
                plex_state = "stopped" if body.state == "stopped" else ("paused" if body.state == "paused" else "playing")
                await client.get(
                    f"{conn.url.rstrip('/')}/:/timeline",
                    params={
                        "ratingKey": cf.source_id,
                        "key": f"/library/metadata/{cf.source_id}",
                        "state": plex_state,
                        "time": str(body.position_ms),
                        "duration": str(body.duration_ms),
                        "identifier": "tv.plex.providers.library",
                        "X-Plex-Token": conn.token,
                    },
                    headers={"Accept": "application/json"},
                )
    except Exception:
        pass  # Best-effort; non-critical

    return {"ok": True}


@router.get("/{type}/{tmdb_id}")
async def get_media_details(
    type: MediaType,
    tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Unified endpoint for Movies and Episodes details."""
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        raise HTTPException(status_code=404, detail="TMDB API Key not configured")

    try:
        # 1. Fetch from TMDB
        if type == MediaType.movie:
            data = await tmdb.get_movie(tmdb_id, api_key=tmdb_key)
        elif type == MediaType.episode:
            # Look up the episode in the local DB to find its show context
            ep_result = await db.execute(
                select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == MediaType.episode)
            )
            local_ep = ep_result.scalars().first()

            if local_ep is None or local_ep.show_id is None:
                raise HTTPException(
                    status_code=404,
                    detail="Episode not found — play it via Plex/Jellyfin first so it can be enriched with show context",
                )

            if local_ep.season_number is None or local_ep.episode_number is None:
                raise HTTPException(
                    status_code=404,
                    detail="Episode is missing season/episode numbers — try refreshing metadata from the show page",
                )

            show_result = await db.execute(select(ShowModel).where(ShowModel.id == local_ep.show_id))
            show = show_result.scalar_one_or_none()
            if show is None:
                raise HTTPException(status_code=404, detail="Show not found for this episode")

            ep_data = await tmdb.get_episode(
                show.tmdb_id, local_ep.season_number, local_ep.episode_number, api_key=tmdb_key
            )
            ep_state: dict = {"tmdb_id": tmdb_id, "type": "episode"}
            await enrich_with_state(db, current_user.id, [ep_state])
            # Check local info for library tags
            library_info = None
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

            return {
                "id": local_ep.id,
                "tmdb_id": tmdb_id,
                "type": "episode",
                "title": ep_data.get("name") or local_ep.title,
                "overview": ep_data.get("overview"),
                "poster_path": tmdb.poster_url(ep_data.get("still_path"), size="w780"),
                "backdrop_path": show.backdrop_path,
                "release_date": ep_data.get("air_date"),
                "tmdb_rating": ep_data.get("vote_average"),
                "runtime": ep_data.get("runtime"),
                "season_number": local_ep.season_number,
                "episode_number": local_ep.episode_number,
                "show_title": show.title,
                "show_tmdb_id": show.tmdb_id,
                "show_poster_path": show.poster_path,
                "show_backdrop_path": show.backdrop_path,
                "cast": [
                    {
                        "tmdb_id": c.get("id"),
                        "name": c.get("name"),
                        "character": c.get("character"),
                        "profile_path": tmdb.poster_url(c.get("profile_path"), size="w185"),
                    }
                    for c in (ep_data.get("credits") or {}).get("cast", [])[:12]
                ],
                "genres": (show.tmdb_data or {}).get("genres", []),
                "in_library": ep_state.get("in_library", False),
                "watched": ep_state.get("watched", False),
                "in_lists": ep_state.get("in_lists", []),
                "user_rating": ep_state.get("user_rating"),
                "library": library_info,
            }
        else:
            raise HTTPException(
                status_code=400, detail="Use /shows/{tmdb_id} for series"
            )

        # 2. Check local info
        query = select(Media).where(Media.tmdb_id == tmdb_id, Media.media_type == type)
        result = await db.execute(query)
        media = result.scalars().first()

        local_info = {"in_library": False, "library": None, "id": None}
        if media:
            local_info["in_library"] = True
            local_info["id"] = media.id
            if not media.adult and data.get("adult", False):
                media.adult = True
                await db.commit()
            coll_q = (
                select(CollectionFile)
                .join(Collection, Collection.id == CollectionFile.collection_id)
                .where(Collection.media_id == media.id, Collection.user_id == current_user.id)
                .order_by(CollectionFile.added_at.desc())
            )
            coll_res = await db.execute(coll_q)
            coll_file = coll_res.scalars().first()
            if coll_file:
                local_info["library"] = {
                    "resolution": coll_file.resolution,
                    "video_codec": coll_file.video_codec,
                    "audio_codec": coll_file.audio_codec,
                    "audio_channels": coll_file.audio_channels,
                    "audio_languages": coll_file.audio_languages,
                    "subtitle_languages": coll_file.subtitle_languages,
                }

        # 3. Format Merged Response
        collection = None
        if type == MediaType.movie:
            raw_coll = data.get("belongs_to_collection")
            if raw_coll:
                coll_data = await tmdb.get_collection(raw_coll["id"], api_key=tmdb_key)
                collection = {
                    "id": coll_data.get("id"),
                    "name": coll_data.get("name"),
                    "poster_path": tmdb.poster_url(coll_data.get("poster_path")),
                    "backdrop_path": tmdb.poster_url(
                        coll_data.get("backdrop_path"), size="original"
                    ),
                    "parts": [
                        {
                            "tmdb_id": p.get("id"),
                            "title": p.get("title"),
                            "type": MediaType.movie,
                            "poster_path": tmdb.poster_url(p.get("poster_path")),
                            "release_date": p.get("release_date"),
                            "overview": p.get("overview"),
                            "adult": p.get("adult", False),
                        }
                        for p in coll_data.get("parts", [])
                    ],
                }

        production_companies = [
            {
                "id": c["id"],
                "name": c["name"],
                "logo_path": tmdb.poster_url(c.get("logo_path"), size="w500")
                if c.get("logo_path")
                else None,
                "origin_country": c.get("origin_country"),
            }
            for c in data.get("production_companies", [])
        ]

        # Enrich with user state (watched, in_lists, collection_pct)
        state_item: dict = {"tmdb_id": tmdb_id, "type": type.value}
        await enrich_with_state(db, current_user.id, [state_item])

        if collection and collection.get("parts"):
            await enrich_with_state(db, current_user.id, collection["parts"])

        where_to_watch = await get_where_to_watch(
            db, current_user.id, tmdb_id, MediaType.movie, media=media, tmdb_key=tmdb_key
        )

        return {
            **local_info,
            "tmdb_id": tmdb_id,
            "type": type,
            "watched": state_item.get("watched", False),
            "in_lists": state_item.get("in_lists", []),
            "user_rating": state_item.get("user_rating"),
            "in_library": state_item.get("in_library", local_info["in_library"]),
            "collection_pct": state_item.get("collection_pct", 100 if local_info["in_library"] else 0),
            "is_monitored": state_item.get("is_monitored", False),
            "request_enabled": state_item.get("request_enabled", False),
            "title": data.get("title") or data.get("name"),
            "original_title": data.get("original_title") or data.get("original_name"),
            "overview": data.get("overview"),
            "poster_path": tmdb.poster_url(data.get("poster_path")),
            "backdrop_path": tmdb.poster_url(
                data.get("backdrop_path"), size="original"
            ),
            "release_date": data.get("release_date") or data.get("first_air_date"),
            "tmdb_rating": data.get("vote_average"),
            "tagline": data.get("tagline"),
            "runtime": data.get("runtime"),
            "status": data.get("status"),
            "genres": [g["name"] for g in data.get("genres", [])],
            "original_language": data.get("original_language"),
            "age_rating": _extract_movie_certification(data),
            "imdb_id": data.get("imdb_id"),
            "adult": data.get("adult", False),
            "collection": collection,
            "production_companies": production_companies,
            "cast": [
                {
                    "tmdb_id": c["id"],
                    "name": c["name"],
                    "character": c["character"],
                    "profile_path": tmdb.poster_url(c["profile_path"]),
                }
                for c in data.get("credits", {}).get("cast", [])[:12]
            ],
            "where_to_watch": where_to_watch,
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=404, detail=f"TMDB Media not found: {e}")


@router.get("/{type}/{tmdb_id}/recommendations")
async def get_media_recommendations(
    type: MediaType,
    tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch movie/series recommendations from TMDB and enrich with state."""
    tmdb_key = await get_user_tmdb_key(db, current_user.id)
    if not check_tmdb_key(tmdb_key):
        return {"results": []}

    try:
        if type == MediaType.movie:
            data = await tmdb.get_movie(tmdb_id, api_key=tmdb_key)
        else:
            data = await tmdb.get_show(tmdb_id, api_key=tmdb_key)
        
        recs_raw = data.get("recommendations", {}).get("results", [])[:12]
        recommendations = [
            {
                "id": None,
                "tmdb_id": r["id"],
                "type": type.value,
                "title": r.get("title") or r.get("name"),
                "original_title": r.get("original_title") or r.get("original_name"),
                "overview": r.get("overview"),
                "poster_path": tmdb.poster_url(r.get("poster_path")),
                "backdrop_path": tmdb.poster_url(r.get("backdrop_path"), size="w1280"),
                "release_date": r.get("release_date") or r.get("first_air_date"),
                "tmdb_rating": r.get("vote_average"),
                "adult": r.get("adult", False),
            }
            for r in recs_raw
        ]
        await enrich_with_state(db, current_user.id, recommendations)
        return {"results": recommendations}
    except Exception:
        return {"results": []}


def _normalize_path(path: str | None, size: str = "w500") -> str | None:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return tmdb.poster_url(path, size=size)


@router.get("/pick")
async def pick_for_me(
    type: str = Query("movie"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import random
    from models.profile import UserProfileData

    if type not in ("movie", "series"):
        raise HTTPException(status_code=400, detail="type must be 'movie' or 'series'")

    tmdb_key = await get_user_tmdb_key(db, current_user.id)

    profile_q = await db.execute(select(UserProfileData).where(UserProfileData.user_id == current_user.id))
    profile = profile_q.scalar_one_or_none()

    conns_q = await db.execute(select(MediaServerConnection).where(MediaServerConnection.user_id == current_user.id))
    connections = conns_q.scalars().all()

    streaming_ids = [int(s) for s in (profile.streaming_services or [])] if profile else []
    region = (profile.country if profile and profile.country else None) or "US"
    has_media_server = bool(connections)

    if not streaming_ids and not has_media_server:
        raise HTTPException(status_code=400, detail="no_sources")

    # ── Watched IDs ────────────────────────────────────────────────────────
    if type == "movie":
        wq = await db.execute(
            select(Media.tmdb_id)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(WatchEvent.user_id == current_user.id, Media.media_type == MediaType.movie)
            .distinct()
        )
    else:
        wq = await db.execute(
            select(ShowModel.tmdb_id)
            .join(Media, Media.show_id == ShowModel.id)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(WatchEvent.user_id == current_user.id)
            .distinct()
        )
    watched_ids: set[int] = {r[0] for r in wq.all()}

    # ── Collection pool (unwatched) ────────────────────────────────────────
    collection_items: list[dict] = []
    if has_media_server:
        if type == "movie":
            cq = await db.execute(
                select(Media.tmdb_id, Media.title, Media.poster_path, Media.backdrop_path,
                       Media.release_date, Media.tmdb_rating, Media.overview)
                .join(Collection, Collection.media_id == Media.id)
                .where(Collection.user_id == current_user.id, Media.media_type == MediaType.movie)
                .where(Media.tmdb_id.notin_(watched_ids))
                .distinct()
            )
            collection_items = [
                {
                    "tmdb_id": r[0], "type": "movie", "title": r[1],
                    "poster_path": _normalize_path(r[2]),
                    "backdrop_path": _normalize_path(r[3], "w1280"),
                    "release_date": r[4], "tmdb_rating": r[5],
                    "overview": r[6], "in_library": True,
                }
                for r in cq.all() if r[0] and r[0] not in watched_ids
            ]
        else:
            cq = await db.execute(
                select(ShowModel.tmdb_id, ShowModel.title, ShowModel.poster_path,
                       ShowModel.backdrop_path, ShowModel.first_air_date,
                       ShowModel.tmdb_rating, ShowModel.overview)
                .join(Media, Media.show_id == ShowModel.id)
                .join(Collection, Collection.media_id == Media.id)
                .where(Collection.user_id == current_user.id)
                .where(ShowModel.tmdb_id.notin_(watched_ids))
                .distinct()
            )
            collection_items = [
                {
                    "tmdb_id": r[0], "type": "series", "title": r[1],
                    "poster_path": _normalize_path(r[2]),
                    "backdrop_path": _normalize_path(r[3], "w1280"),
                    "release_date": r[4], "tmdb_rating": r[5],
                    "overview": r[6], "in_library": True,
                }
                for r in cq.all() if r[0] and r[0] not in watched_ids
            ]

    # ── Streaming pool (progressive fallback) ─────────────────────────────
    streaming_candidates: list[dict] = []
    if streaming_ids and check_tmdb_key(tmdb_key):
        user_genres = ((profile.movie_genres if type == "movie" else profile.show_genres) or []) if profile else []
        genre_map = MOVIE_GENRE_IDS if type == "movie" else TV_GENRE_IDS
        genre_ids = [genre_map[g] for g in user_genres if g in genre_map]

        tiers = [
            {"genre_ids": genre_ids[:3], "min_rating": 6.0},
            {"genre_ids": [], "min_rating": 6.0},
            {"genre_ids": [], "min_rating": None},
        ]

        for tier in tiers:
            if len(streaming_candidates) + len(collection_items) >= 15:
                break
            coros = []
            for pid in streaming_ids:
                kwargs: dict = dict(watch_provider_id=pid, watch_region=region, api_key=tmdb_key)
                if tier["min_rating"]:
                    kwargs["min_rating"] = tier["min_rating"]
                if tier["genre_ids"]:
                    for gid in tier["genre_ids"]:
                        fn = tmdb.discover_movies if type == "movie" else tmdb.discover_shows
                        coros.append(fn(genre_id=gid, **kwargs))
                else:
                    fn = tmdb.discover_movies if type == "movie" else tmdb.discover_shows
                    coros.append(fn(**kwargs))

            if coros:
                results_list = await asyncio.gather(*coros, return_exceptions=True)
                for res in results_list:
                    if isinstance(res, Exception):
                        continue
                    for r in res.get("results", []):
                        tid = r.get("id")
                        if tid and tid not in watched_ids:
                            streaming_candidates.append(r)

    # ── Combine & deduplicate ──────────────────────────────────────────────
    genre_id_map = {v: k for k, v in (MOVIE_GENRE_IDS if type == "movie" else TV_GENRE_IDS).items()}

    seen: set[int] = set()
    all_candidates: list[dict] = []

    for item in collection_items:
        if item["tmdb_id"] not in seen:
            seen.add(item["tmdb_id"])
            all_candidates.append(item)

    for r in streaming_candidates:
        tid = r.get("id")
        if tid and tid not in seen and tid not in watched_ids:
            seen.add(tid)
            all_candidates.append({
                "tmdb_id": tid,
                "type": type,
                "title": r.get("title") if type == "movie" else r.get("name"),
                "poster_path": tmdb.poster_url(r.get("poster_path")),
                "backdrop_path": tmdb.poster_url(r.get("backdrop_path"), size="w1280"),
                "release_date": r.get("release_date") if type == "movie" else r.get("first_air_date"),
                "tmdb_rating": r.get("vote_average"),
                "overview": r.get("overview"),
                "in_library": False,
                "genres": [genre_id_map[gid] for gid in r.get("genre_ids", []) if gid in genre_id_map],
                "adult": r.get("adult", False),
            })

    if not all_candidates:
        raise HTTPException(status_code=404, detail="no_results")

    pick = random.choice(all_candidates)

    # ── Fetch genres from local DB for the picked item ─────────────────────
    if not pick.get("genres"):
        if type == "movie":
            genres_q = await db.execute(
                select(Media.tmdb_data).where(
                    Media.tmdb_id == pick["tmdb_id"], Media.media_type == MediaType.movie
                ).limit(1)
            )
        else:
            genres_q = await db.execute(
                select(ShowModel.tmdb_data).where(ShowModel.tmdb_id == pick["tmdb_id"]).limit(1)
            )
        row = genres_q.scalar_one_or_none()
        if row:
            pick["genres"] = (row or {}).get("genres", [])

    # ── Enrich pick: overview + watch providers ────────────────────────────
    sources: list[dict] = []
    if check_tmdb_key(tmdb_key):
        try:
            if not pick.get("overview") or not pick.get("genres"):
                if type == "movie":
                    details = await tmdb.get_movie(pick["tmdb_id"], api_key=tmdb_key)
                else:
                    details = await tmdb.get_show(pick["tmdb_id"], api_key=tmdb_key)
                if not pick.get("overview"):
                    pick["overview"] = details.get("overview")
                if not pick.get("genres"):
                    pick["genres"] = [g["name"] for g in details.get("genres", [])]

            if type == "movie":
                providers_data = await tmdb.get_movie_watch_providers(pick["tmdb_id"], api_key=tmdb_key)
            else:
                providers_data = await tmdb.get_show_watch_providers(pick["tmdb_id"], api_key=tmdb_key)

            region_providers = providers_data.get("results", {}).get(region, {})
            flatrate = region_providers.get("flatrate", [])
            str_streaming_ids = [str(s) for s in streaming_ids]
            for p in flatrate:
                if str(p.get("provider_id", "")) in str_streaming_ids:
                    sources.append({
                        "type": "streaming",
                        "name": p.get("provider_name"),
                        "logo": f"https://image.tmdb.org/t/p/w45{p['logo_path']}" if p.get("logo_path") else None,
                    })
        except Exception:
            pass

    if pick.get("in_library"):
        seen_conn_types: set[str] = set()
        for c in connections:
            if c.type not in seen_conn_types:
                seen_conn_types.add(c.type)
                sources.append({"type": c.type, "name": c.name or c.type.title(), "logo": None})

    pick["sources"] = sources
    return pick
