import asyncio
from collections import defaultdict
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_
from db import get_db, AsyncSessionLocal
from core import tmdb as tmdb_client
from core.translations import upsert_media_translation, upsert_show_translation
from dependencies import get_current_user, get_current_user_or_api_key
from models.users import User
from models.profile import UserProfileData
from models.events import WatchEvent
from models.collection import Collection
from models.media import Media
from models.show import Show as ShowModel
from models.lists import List as ListModel, ListItem


router = APIRouter()


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
