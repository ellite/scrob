import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import get_db, engine
from dependencies import get_current_user
from models.base import CollectionSource
from models.global_settings import GlobalSettings
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings
from core.data_export import build_export_zip
from core.scrob_import import MAX_TOTAL_SIZE, apply_scrob_import, parse_scrob_export

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/data")
async def export_data(
    watched: bool = Query(True),
    ratings: bool = Query(True),
    collection: bool = Query(True),
    lists: bool = Query(True),
    comments: bool = Query(True),
    api_keys: bool = Query(False),
    media_connections: bool = Query(False),
    scrobble_connections: bool = Query(False),
    connections: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Personal data export — watch history, ratings, collection, lists, and
    comments as a zip using the same file/shape layout as a Trakt.tv export,
    so it can be read back in by the existing Trakt-export import flow.
    Each category can be excluded via its query param.

    api_keys/media_connections/scrobble_connections/connections are opt-in
    only (default off) and, when requested, include plaintext secrets
    (API keys, OAuth tokens, media-server tokens) — the caller is
    responsible for warning the user before requesting them."""
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    settings = settings_result.scalar_one_or_none()

    payload = await build_export_zip(
        db, current_user, settings,
        include_watched=watched,
        include_ratings=ratings,
        include_collection=collection,
        include_lists=lists,
        include_comments=comments,
        include_api_keys=api_keys,
        include_media_connections=media_connections,
        include_scrobble_connections=scrobble_connections,
        include_connections=connections,
    )
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"scrob-export-{current_user.username}-{timestamp}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
        },
    )


async def run_scrob_import(user_id: int, job_id: int, data, include: dict) -> None:
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
            logger.exception("Scrob import job %s failed", job_id)
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.failed, error_message=str(exc)))
            await db.commit()


@router.post("/import")
async def import_data(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    watched: bool = Form(True),
    ratings: bool = Form(True),
    collection: bool = Form(True),
    lists: bool = Form(True),
    comments: bool = Form(True),
    api_keys: bool = Form(False),
    media_connections: bool = Form(False),
    scrobble_connections: bool = Form(False),
    connections: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Restore a Scrob data export (from GET /export/data) into this account.
    What to import is chosen per-upload, same as the Trakt export import."""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip export files are accepted.")

    include = {
        "include_watched": watched, "include_ratings": ratings, "include_collection": collection,
        "include_lists": lists, "include_comments": comments, "include_api_keys": api_keys,
        "include_media_connections": media_connections, "include_scrobble_connections": scrobble_connections,
        "include_connections": connections,
    }
    if not any(include.values()):
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
        data = parse_scrob_export(content)
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

    job = SyncJob(user_id=current_user.id, source=CollectionSource.manual, status=SyncStatus.pending, job_type="import")
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_scrob_import, current_user.id, job.id, data, include)
    return {"status": "started", "job_id": job.id, "message": "Scrob import is running in the background"}
