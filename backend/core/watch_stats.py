from typing import NamedTuple

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.media_runtime import effective_runtime_expression
from models.events import WatchEvent
from models.media import Media


class WatchStats(NamedTuple):
    plays: int
    watch_time_minutes: int
    last_watched_at: str | None


async def get_watch_stats(
    db: AsyncSession,
    user_id: int,
    *,
    media_ids: list[int] | None = None,
    tmdb_id: int | None = None,
    media_type: str | None = None,
    show_id: int | None = None,
) -> WatchStats:
    runtime = effective_runtime_expression()
    watch_time = func.coalesce(
        func.sum(case((WatchEvent.completed == True, runtime), else_=0)),
        0,
    )
    last_watched_at = func.max(case((WatchEvent.completed == True, WatchEvent.watched_at)))
    query = (
        # A play is a completed watch: in-progress rows (a Jellyfin/Emby pull
        # stores them with completed=False) are not plays, the same as for
        # watch time and the last watched date.
        select(func.count(case((WatchEvent.completed == True, WatchEvent.id))), watch_time, last_watched_at)
        .select_from(Media)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id)
    )
    if media_ids is not None:
        if not media_ids:
            return WatchStats(0, 0, None)
        query = query.where(Media.id.in_(media_ids))
    if tmdb_id is not None:
        query = query.where(Media.tmdb_id == tmdb_id)
    if media_type is not None:
        query = query.where(Media.media_type == media_type)
    if show_id is not None:
        query = query.where(Media.show_id == show_id, Media.media_type == "episode")

    row = (await db.execute(query)).one()
    return WatchStats(
        int(row[0] or 0),
        int(row[1] or 0),
        row[2].isoformat() if row[2] else None,
    )
