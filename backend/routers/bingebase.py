import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import get_db
from models import CollectionSource, Media, Show, SyncJob, SyncStatus, User, UserSettings, WatchEvent
from models.sync import SyncCancelled, _raise_if_cancelled
from routers.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


async def run_bingebase_push(user_id: int, job_id: int) -> None:
    """Push all historical watched events from Scrob DB to Bingebase Webhook URL."""
    from database import AsyncSessionLocal
    from routers.webhooks import _maybe_bingebase_scrobble

    async with AsyncSessionLocal() as db:
        try:
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.in_progress)
            )
            await db.commit()

            result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = result.scalar_one_or_none()
            if not settings or not settings.bingebase_webhook_url:
                raise ValueError("Configure a valid Bingebase Webhook URL in Settings first")

            events_result = await db.execute(
                select(WatchEvent)
                .options(selectinload(WatchEvent.media))
                .where(WatchEvent.user_id == user_id)
                .order_by(WatchEvent.watched_at.asc())
            )
            events = events_result.scalars().all()
            total_items = len(events)

            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(total_items=total_items)
            )
            await db.commit()

            print(f"Bingebase push job {job_id}: queued {total_items} watched items.")

            processed_so_far = 0
            for i, event in enumerate(events, 1):
                if event.media:
                    await _maybe_bingebase_scrobble(settings, event.media, "stop", 1.0, db=db)

                processed_so_far = i
                if i % 10 == 0 or i == total_items:
                    await db.execute(
                        update(SyncJob).where(SyncJob.id == job_id).values(processed_items=processed_so_far)
                    )
                    await db.commit()
                    await _raise_if_cancelled(db, job_id)

                await asyncio.sleep(0.05)  # Slight delay to avoid overwhelming webhook server

            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.completed,
                    processed_items=processed_so_far,
                    stats={"submitted": processed_so_far},
                )
            )
            await db.commit()
            print(f"Bingebase push job {job_id} completed: {processed_so_far} items pushed.")
        except SyncCancelled:
            logger.info("Bingebase push job %s cancelled", job_id)
            await db.rollback()
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.cancelled,
                    processed_items=processed_so_far,
                )
            )
            await db.commit()
        except Exception as exc:
            logger.exception("Bingebase push job %s failed", job_id)
            await db.rollback()
            await db.execute(
                update(SyncJob).where(SyncJob.id == job_id).values(
                    status=SyncStatus.failed,
                    error_message=str(exc),
                )
            )
            await db.commit()


@router.post("/push")
async def push_bingebase(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger a full background push of all historical watched items to Bingebase."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = result.scalar_one_or_none()

    if not settings or not settings.bingebase_webhook_url:
        raise HTTPException(
            status_code=400,
            detail="Configure a valid Bingebase Webhook URL in Settings first",
        )

    job = SyncJob(
        user_id=current_user.id,
        source=CollectionSource.bingebase,
        status=SyncStatus.pending,
        job_type="push",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(run_bingebase_push, current_user.id, job.id)
    return {"status": "started", "job_id": job.id, "message": "Bingebase push is running in the background"}
