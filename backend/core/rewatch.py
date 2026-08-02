from sqlalchemy import select, func, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.rewatch import ShowRewatch, RewatchProgress
from models.media import Media
from models.show import Show as ShowModel
from models.base import MediaType


def capped_season_episode_counts(show: ShowModel, tmdb_extra: dict | None = None) -> dict[int, int]:
    """Per-season episode counts from cached (or freshly-fetched) TMDB metadata,
    capped at the last aired episode for shows still airing. Mirrors the
    season_ep_counts calculation in routers.shows.get_show."""
    season_ep_counts: dict[int, int] = {
        s["season_number"]: s.get("episode_count", 0)
        for s in (show.tmdb_data or {}).get("seasons", [])
    }

    last_ep = (tmdb_extra or show.tmdb_data or {}).get("last_episode_to_air")
    if last_ep:
        last_sn = last_ep.get("season_number")
        last_en = last_ep.get("episode_number")
        if last_sn in season_ep_counts:
            season_ep_counts[last_sn] = last_en
        for sn in season_ep_counts:
            if sn > last_sn:
                season_ep_counts[sn] = 0

    return season_ep_counts


def total_aired_episodes(show: ShowModel) -> int:
    """Total non-special aired episode count for a show, using only data
    already cached on the Show row (no TMDB call). For shows still airing
    this may lag behind reality until metadata is next refreshed - an
    accepted tradeoff so recording a watch never triggers a live API call."""
    counts = capped_season_episode_counts(show)
    return sum(v for sn, v in counts.items() if sn != 0)


async def get_active_rewatch(db: AsyncSession, user_id: int, show_id: int) -> ShowRewatch | None:
    result = await db.execute(
        select(ShowRewatch).where(ShowRewatch.user_id == user_id, ShowRewatch.show_id == show_id)
    )
    return result.scalar_one_or_none()


async def get_active_rewatches_for_shows(
    db: AsyncSession, user_id: int, show_ids: list[int]
) -> dict[int, ShowRewatch]:
    if not show_ids:
        return {}
    result = await db.execute(
        select(ShowRewatch).where(ShowRewatch.user_id == user_id, ShowRewatch.show_id.in_(show_ids))
    )
    return {r.show_id: r for r in result.scalars().all()}


async def get_rewatch_progress_media_ids(db: AsyncSession, rewatch_id: int) -> set[int]:
    result = await db.execute(
        select(RewatchProgress.media_id).where(RewatchProgress.rewatch_id == rewatch_id)
    )
    return {row[0] for row in result.all()}


async def record_rewatch_progress(db: AsyncSession, user_id: int, media_id: int, watch_event_id: int) -> None:
    """Called after a completed WatchEvent is flushed (has an id) for an
    episode. No-ops unless that episode's show has an active rewatch for
    this user. Only flushes - never commits - so it composes with whatever
    transaction the caller is already managing around the WatchEvent write."""
    media_result = await db.execute(select(Media).where(Media.id == media_id))
    media = media_result.scalar_one_or_none()
    if not media or media.media_type != MediaType.episode or not media.show_id:
        return

    rewatch = await get_active_rewatch(db, user_id, media.show_id)
    if not rewatch:
        return

    stmt = pg_insert(RewatchProgress).values(
        rewatch_id=rewatch.id, media_id=media_id, watch_event_id=watch_event_id
    )
    stmt = stmt.on_conflict_do_nothing(constraint="uq_rewatch_progress_rewatch_media")
    await db.execute(stmt)
    await db.flush()

    await _maybe_complete_rewatch(db, rewatch)


async def _maybe_complete_rewatch(db: AsyncSession, rewatch: ShowRewatch) -> None:
    show_result = await db.execute(select(ShowModel).where(ShowModel.id == rewatch.show_id))
    show = show_result.scalar_one_or_none()
    if not show:
        return

    total = total_aired_episodes(show)
    if total <= 0:
        return

    progress_count_result = await db.execute(
        select(func.count()).where(RewatchProgress.rewatch_id == rewatch.id)
    )
    progress_count = progress_count_result.scalar() or 0

    if progress_count >= total:
        await db.execute(delete(ShowRewatch).where(ShowRewatch.id == rewatch.id))
        await db.flush()
