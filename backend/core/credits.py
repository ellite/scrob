"""Background TMDB credits import and people/studio stat aggregation.

Backs the "Most Watched Actors" / "Favorite Directors" / "Favorite Writers" /
"Favorite Studios" groups on the profile stats page (#124). Credits are the
same for every user, so they live in one instance-wide title_credits table,
refreshed in the background at most once per TTL. The stats page triggers the
backfill on view (own profile only) and the sections simply stay hidden until
data is available.
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.show import Show
from models.title_credits import TitleCredits

CREDITS_TTL = timedelta(days=7)
FETCH_CONCURRENCY = 8

# TMDB crew jobs that count as "writer" for the stats.
WRITER_JOBS = {"Writer", "Screenplay", "Story", "Teleplay"}

_importing = False


def _people(raw: list, jobs: set | None = None, limit: int | None = None) -> list[dict]:
    """Reduce a TMDB cast/crew/company list to unique {id, name} pairs."""
    out, seen = [], set()
    for p in raw or []:
        if jobs is not None and p.get("job") not in jobs:
            continue
        pid = p.get("id")
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        out.append({"id": pid, "name": p.get("name")})
        if limit and len(out) >= limit:
            break
    return out


async def _import_credits(api_key: str) -> None:
    """Fetch cast/crew/studios for every watched movie/show missing from
    title_credits (or older than the TTL). Own DB session; runs in background.
    get_movie/get_show already append credits, so this costs one TMDB call per
    title and nothing extra per user."""
    from core import tmdb as tmdb_client
    from db import engine

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        movie_rows = (await db.execute(
            select(Media.tmdb_id).distinct()
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(Media.media_type == MediaType.movie, Media.tmdb_id.isnot(None))
        )).all()
        show_rows = (await db.execute(
            select(Show.tmdb_id).distinct()
            .join(Media, Media.show_id == Show.id)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(Show.tmdb_id.isnot(None))
        )).all()
        wanted = [("movie", r[0]) for r in movie_rows] + [("series", r[0]) for r in show_rows]

        existing = {
            (mt, tid): fetched
            for tid, mt, fetched in (await db.execute(
                select(TitleCredits.tmdb_id, TitleCredits.media_type, TitleCredits.fetched_at)
            )).all()
        }
        cutoff = datetime.utcnow() - CREDITS_TTL
        todo = [(mt, tid) for (mt, tid) in wanted
                if (mt, tid) not in existing or existing[(mt, tid)] < cutoff]
        if not todo:
            return

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _one(mt: str, tid: int):
            async with sem:
                try:
                    if mt == "movie":
                        data = await tmdb_client.get_movie(tid, api_key=api_key)
                    else:
                        data = await tmdb_client.get_show(tid, api_key=api_key)
                except Exception:
                    return None
                credits = data.get("credits") or {}
                cast = _people(credits.get("cast"), limit=10)
                directors = _people(credits.get("crew"), jobs={"Director"})
                writers = _people(credits.get("crew"), jobs=WRITER_JOBS)
                if mt == "series" and not writers:
                    writers = _people(data.get("created_by"))
                studios = _people(data.get("production_companies"))
                return mt, tid, cast, directors, writers, studios

        results = await asyncio.gather(*[_one(mt, tid) for mt, tid in todo])
        for res in results:
            if not res:
                continue
            mt, tid, cast, directors, writers, studios = res
            row = (await db.execute(
                select(TitleCredits).where(TitleCredits.tmdb_id == tid, TitleCredits.media_type == mt)
            )).scalars().first()
            if row:
                row.cast, row.directors, row.writers, row.studios = cast, directors, writers, studios
                row.fetched_at = datetime.utcnow()
            else:
                db.add(TitleCredits(tmdb_id=tid, media_type=mt, cast=cast,
                                    directors=directors, writers=writers, studios=studios))
        await db.commit()


async def _background_import(api_key: str) -> None:
    # _importing is claimed by the caller (maybe_backfill_credits) before this
    # is scheduled, so ownership of clearing it belongs here regardless of outcome.
    global _importing
    try:
        await _import_credits(api_key)
    except Exception as e:
        print(f"Credits import failed: {e}")
    finally:
        _importing = False


async def maybe_backfill_credits(db: AsyncSession, user_id: int) -> None:
    """Kick off the credits backfill in the background when the table is empty
    or stale. Non-blocking: the stats page fills the people/studio groups on
    the next load once the import finishes."""
    global _importing
    if _importing:
        return
    newest = (await db.execute(select(func.max(TitleCredits.fetched_at)))).scalar_one_or_none()
    if newest and (datetime.utcnow() - newest) < CREDITS_TTL:
        return
    from routers.media import check_tmdb_key, get_user_tmdb_key

    api_key = await get_user_tmdb_key(db, user_id)
    if not check_tmdb_key(api_key):
        return
    # Re-check right before claiming: two concurrent callers can both pass the
    # checks above (each awaits a query in between), so the actual claim has to
    # happen in this uninterrupted, awaitless stretch to stay race-free.
    if _importing:
        return
    _importing = True
    asyncio.create_task(_background_import(api_key))


async def credits_stats(db: AsyncSession, user_id: int, date_filters: list) -> dict:
    """Aggregate watched titles' cast/crew/studios into ranked lists. Each
    title contributes its people once; ranked by distinct titles then plays.
    date_filters are the same WatchEvent filters the stats endpoint uses."""
    rows = (await db.execute(
        select(
            Media.media_type, Media.tmdb_id, Show.tmdb_id.label("show_tmdb"),
            func.count(WatchEvent.id).label("plays"),
        )
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .outerjoin(Show, Media.show_id == Show.id)
        .where(WatchEvent.user_id == user_id, *date_filters)
        .group_by(Media.media_type, Media.tmdb_id, Show.tmdb_id)
    )).all()

    # Weight per (media_type, tmdb): a movie by its own tmdb, an episode by its show.
    weight: dict[tuple, int] = defaultdict(int)
    for media_type, tmdb_id, show_tmdb, plays in rows:
        if media_type == MediaType.movie and tmdb_id:
            weight[("movie", tmdb_id)] += plays
        elif media_type == MediaType.episode and show_tmdb:
            weight[("series", show_tmdb)] += plays
    empty = {"actors": [], "directors": [], "writers": [], "studios": []}
    if not weight:
        return empty

    credits = (await db.execute(select(TitleCredits))).scalars().all()
    by_key = {(c.media_type, c.tmdb_id): c for c in credits}

    buckets = {"actors": {}, "directors": {}, "writers": {}, "studios": {}}

    def _add(bucket: str, person: dict, plays: int):
        pid = person.get("id")
        if pid is None:
            return
        e = buckets[bucket].setdefault(pid, {"name": person.get("name"), "titles": 0, "plays": 0})
        e["titles"] += 1
        e["plays"] += plays

    for key, plays in weight.items():
        c = by_key.get(key)
        if not c:
            continue
        for p in c.cast or []:
            _add("actors", p, plays)
        for p in c.directors or []:
            _add("directors", p, plays)
        for p in c.writers or []:
            _add("writers", p, plays)
        for p in c.studios or []:
            _add("studios", p, plays)

    def _top(bucket: str, limit: int = 15) -> list[dict]:
        return sorted(
            ({"id": pid, **v} for pid, v in buckets[bucket].items()),
            key=lambda x: (x["titles"], x["plays"]), reverse=True,
        )[:limit]

    return {k: _top(k) for k in buckets}
