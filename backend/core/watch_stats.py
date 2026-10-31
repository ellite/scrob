from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Integer as SAInteger, Text as SAText

from models.events import WatchEvent
from models.media import Media


# PostgreSQL JSON needs a text cast, and JSON null must be cleared before the integer cast.
def effective_runtime_expression():
    json_runtime = func.cast(
        func.nullif(func.cast(Media.tmdb_data["runtime"], SAText), "null"),
        SAInteger,
    )
    return func.coalesce(Media.runtime, json_runtime)


async def get_watch_stats(
    db: AsyncSession,
    user_id: int,
    *,
    media_ids: list[int] | None = None,
    tmdb_id: int | None = None,
    media_type: str | None = None,
    show_id: int | None = None,
) -> tuple[int, int]:
    runtime = effective_runtime_expression()
    watch_time = func.coalesce(
        func.sum(case((WatchEvent.completed == True, runtime), else_=0)),
        0,
    )
    query = (
        select(func.count(WatchEvent.id), watch_time)
        .select_from(Media)
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(WatchEvent.user_id == user_id)
    )
    if media_ids is not None:
        if not media_ids:
            return 0, 0
        query = query.where(Media.id.in_(media_ids))
    if tmdb_id is not None:
        query = query.where(Media.tmdb_id == tmdb_id)
    if media_type is not None:
        query = query.where(Media.media_type == media_type)
    if show_id is not None:
        query = query.where(Media.show_id == show_id, Media.media_type == "episode")

    row = (await db.execute(query)).one()
    return int(row[0] or 0), int(row[1] or 0)
