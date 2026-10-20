from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.sync import SyncJob, SyncStatus

RETENTION = timedelta(days=7)


async def prune_old_sync_jobs(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete finished sync jobs older than RETENTION and return how many went.

    Every sync run keeps its own job row and the warnings it produced, which
    grew without bound and made the endpoints that read them exhaust memory
    (#489). Nothing needs a finished job older than a week, with one exception:
    the auto-sync scheduler derives each connection's next run from its newest
    completed/failed job, so that one is kept per connection and job type -
    without it a long interval would fire again immediately after a prune.
    """
    cutoff = (now or datetime.now(timezone.utc).replace(tzinfo=None)) - RETENTION
    ranked = (
        select(
            SyncJob.id.label("id"),
            func.row_number().over(
                partition_by=(SyncJob.user_id, SyncJob.source, SyncJob.connection_id, SyncJob.job_type),
                order_by=(SyncJob.updated_at.desc(), SyncJob.id.desc()),
            ).label("rn"),
        )
        .where(SyncJob.status.in_([SyncStatus.completed, SyncStatus.failed]))
        .subquery()
    )
    newest = select(ranked.c.id).where(ranked.c.rn == 1)
    result = await db.execute(
        delete(SyncJob).where(
            SyncJob.status.in_([SyncStatus.completed, SyncStatus.failed, SyncStatus.cancelled]),
            SyncJob.updated_at < cutoff,
            SyncJob.id.not_in(newest),
        )
    )
    await db.commit()
    return result.rowcount or 0
