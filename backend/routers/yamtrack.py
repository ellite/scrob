import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import get_db, engine
from dependencies import get_current_user
from models.base import CollectionSource
from models.global_settings import GlobalSettings
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings
from core.scrob_import import MAX_TOTAL_SIZE, ScrobImportData, apply_scrob_import
from core.yamtrack_import import parse_yamtrack_csv

logger = logging.getLogger(__name__)

router = APIRouter()


async def run_yamtrack_import(user_id: int, job_id: int, data: ScrobImportData, include: dict) -> None:
    """Applies a parsed Yamtrack/Floppy CSV export in the background.

    Structurally identical to routers/export.py's run_scrob_import - both
    are thin wrappers around apply_scrob_import, which doesn't care whether
    the ScrobImportData it's handed came from a Scrob backup or a
    translated CSV."""
    from routers.sync import SyncCancelled

    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running))
            await db.commit()

            settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            settings = settings_result.scalar_one_or_none()
            api_key = settings.tmdb_api_key if settings else None
            if not api_key:
                gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
                gs = gs_result.scalar_one_or_none()
                api_key = gs.tmdb_api_key if gs else None

            stats = await apply_scrob_import(db, job_id, user_id, data, api_key, **include)

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.completed, stats=stats))
            await db.commit()
        except SyncCancelled:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.cancelled))
            await db.commit()
        except Exception as exc:
            logger.exception("Yamtrack import job %s failed", job_id)
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=str(exc)))
            await db.commit()


@router.post("/import/upload")
async def yamtrack_import_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    watched: bool = Form(True),
    ratings: bool = Form(True),
    collection: bool = Form(True),
    lists: bool = Form(True),
    comments: bool = Form(True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Import watch history, ratings, collection, lists, and comments from a
    Yamtrack (or Floppy, a Yamtrack fork) CSV export. What to import is
    chosen per-upload, same as the Scrob and Trakt export imports."""
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv export files are accepted.")

    include = {
        "include_watched": watched, "include_ratings": ratings, "include_collection": collection,
        "include_lists": lists, "include_comments": comments,
        "include_api_keys": False, "include_media_connections": False,
        "include_scrobble_connections": False, "include_connections": False,
    }
    if not any([watched, ratings, collection, lists, comments]):
        raise HTTPException(status_code=400, detail="Select at least one item to import.")

    chunks: list[bytes] = []
    total_read = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > MAX_TOTAL_SIZE:
            raise HTTPException(status_code=413, detail="Export file is too large to import.")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        data = parse_yamtrack_csv(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()
    _tmdb_key = settings.tmdb_api_key if settings else None
    if not _tmdb_key:
        gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        gs = gs_result.scalar_one_or_none()
        _tmdb_key = gs.tmdb_api_key if gs else None
    if not _tmdb_key:
        raise HTTPException(status_code=400, detail="TMDB API key required for import")

    job = SyncJob(user_id=current_user.id, source=CollectionSource.yamtrack, status=SyncStatus.pending, job_type="import")
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_yamtrack_import, current_user.id, job.id, data, include)
    return {"status": "started", "job_id": job.id, "message": "Yamtrack import is running in the background"}
