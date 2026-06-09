import gzip
import io
import json
import struct
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.future import select
from sqlalchemy import delete, func, update

from db import get_db, engine
from models.users import User
from models.global_settings import GlobalSettings
from models.media import Media
from models.collection import Collection
from models.sync import SyncJob, SyncStatus
from models.base import CollectionSource, MediaType
from models.media_request import MediaRequest, RequestStatus
from models.users import UserSettings
from dependencies import require_admin
from core.url_validator import validate_service_url
from core.backup import asyncpg_conn, restore_backup
import schemas

router = APIRouter()


async def _get_or_create_global_settings(db: AsyncSession) -> GlobalSettings:
    result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = result.scalar_one_or_none()
    if not gs:
        gs = GlobalSettings(id=1)
        db.add(gs)
        await db.flush()
    return gs


@router.get("/settings", response_model=schemas.GlobalSettings)
async def get_global_settings(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    return await _get_or_create_global_settings(db)


@router.patch("/settings", response_model=schemas.GlobalSettings)
async def update_global_settings(
    body: schemas.GlobalSettings,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    gs = await _get_or_create_global_settings(db)

    update_data = body.model_dump(exclude_unset=True)

    url_fields = {"radarr_url": "Radarr URL", "sonarr_url": "Sonarr URL"}
    for field, label in url_fields.items():
        if field in update_data and update_data[field]:
            update_data[field] = await validate_service_url(update_data[field], label)

    for field, value in update_data.items():
        if hasattr(gs, field):
            setattr(gs, field, value)

    await db.commit()
    await db.refresh(gs)
    return gs


@router.get("/users", response_model=list[schemas.AdminUser])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.created_at.asc()))
    return result.scalars().all()


@router.patch("/users/{user_id}/toggle-admin", response_model=schemas.AdminUser)
async def toggle_admin(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent removing your own admin if you're the only one
    if target.id == current_user.id and target.is_admin:
        count_result = await db.execute(
            select(func.count()).select_from(User).where(User.is_admin.is_(True))
        )
        if count_result.scalar_one() <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You are the sole admin. Promote another user before removing your own admin rights.",
            )

    target.is_admin = not target.is_admin
    await db.commit()
    await db.refresh(target)
    return target


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if user_id == current_user.id:
        count_result = await db.execute(select(func.count()).select_from(User))
        if count_result.scalar_one() > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot delete your own account while other users exist.",
            )

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
    return {"status": "deleted"}


@router.get("/backup")
async def backup_database(_: User = Depends(require_admin)):
    conn = await asyncpg_conn()
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename != 'alembic_version'"
        )
        tables = [r["tablename"] for r in rows]

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            header = json.dumps({"version": 1, "tables": tables}).encode()
            gz.write(struct.pack(">I", len(header)))
            gz.write(header)
            for table in tables:
                data_buf = io.BytesIO()
                await conn.copy_from_table(table, output=data_buf, format="binary")
                data = data_buf.getvalue()
                name_bytes = table.encode()
                gz.write(struct.pack(">H", len(name_bytes)))
                gz.write(name_bytes)
                gz.write(struct.pack(">Q", len(data)))
                gz.write(data)

        payload = buf.getvalue()
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"scrob_backup_{timestamp}.bak"
        return Response(
            content=payload,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(payload)),
            },
        )
    finally:
        await conn.close()


@router.get("/maintenance/cache-stats")
async def get_cache_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    from models.image_cache import ImageCache
    total_size = (await db.execute(select(func.sum(ImageCache.file_size)))).scalar() or 0
    count = (await db.execute(select(func.count(ImageCache.id)))).scalar() or 0
    gs = (await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))).scalar_one_or_none()
    return {
        "total_size_bytes": total_size,
        "entry_count": count,
        "limit_gb": gs.image_cache_limit_gb if gs else None,
    }


@router.post("/maintenance/clear-cache")
async def admin_clear_cache(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete all cached TMDB images on disk and clear their database records."""
    from core.image_cache import clear_image_cache
    await clear_image_cache(db)
    return {"status": "success", "message": "Image cache cleared successfully"}


@router.post("/maintenance/heal")
async def admin_heal_metadata(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Re-enrich all collection items server-wide that are missing poster/date metadata."""
    gs = await _get_or_create_global_settings(db)
    if not gs.tmdb_api_key:
        raise HTTPException(status_code=400, detail="A global TMDB API key is required for server-wide heal")
    job = SyncJob(user_id=current_user.id, source=CollectionSource.tmdb, job_type="heal", status=SyncStatus.pending)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(run_admin_heal, gs.tmdb_api_key, current_user.id, job.id)
    return {"status": "started", "message": "Server-wide metadata heal is running in the background"}


async def run_admin_heal(api_key: str, user_id: int | None = None, job_id: int | None = None):
    from models.show import Show
    from routers.sync import batch_enrich_items
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        async def _update_job(**kwargs):
            if job_id is None:
                return
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(updated_at=func.now(), **kwargs))
            await db.commit()

        try:
            await _update_job(status=SyncStatus.running)

            coll_q = await db.execute(
                select(Media)
                .where(
                    Media.poster_path.is_(None),
                    Media.id.in_(select(Collection.media_id).distinct()),
                )
            )
            items = coll_q.scalars().all()

            movies = [m for m in items if m.media_type == MediaType.movie and m.tmdb_id]
            episodes = [m for m in items if m.media_type == MediaType.episode and m.show_id and m.season_number is not None and m.episode_number is not None]

            if not movies and not episodes:
                print("Admin heal: nothing to fix server-wide")
                await _update_job(status=SyncStatus.completed, total_items=0, stats={"healed": True})
                return

            print(f"Admin heal: {len(movies)} movies, {len(episodes)} episodes to re-enrich")

            show_ids = list({m.show_id for m in episodes})
            show_tmdb_map: dict[int, int] = {}
            if show_ids:
                shows_q = await db.execute(select(Show).where(Show.id.in_(show_ids)))
                for s in shows_q.scalars().all():
                    if s.tmdb_id:
                        show_tmdb_map[s.id] = s.tmdb_id

            to_enrich = [(m, None) for m in movies] + [
                (m, show_tmdb_map[m.show_id]) for m in episodes if m.show_id in show_tmdb_map
            ]

            await _update_job(total_items=len(to_enrich), processed_items=0)
            await batch_enrich_items(to_enrich, api_key=api_key)
            await db.commit()
            await _update_job(processed_items=len(to_enrich), status=SyncStatus.completed, stats={"healed": True})
            print(f"Admin heal complete: processed {len(to_enrich)} items")
        except Exception as e:
            print(f"Admin heal failed: {e}")
            import traceback
            traceback.print_exc()
            await _update_job(status=SyncStatus.failed, error_message=str(e)[:900])


@router.post("/restore")
async def restore_database(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    if not (file.filename or "").endswith(".bak"):
        raise HTTPException(status_code=400, detail="Only .bak backup files are accepted.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Release the SQLAlchemy session's open transaction before the raw asyncpg
    # TRUNCATE. get_current_user (via require_admin) holds ACCESS SHARE on `users`
    # for the duration of the transaction; TRUNCATE needs ACCESS EXCLUSIVE on all
    # tables and would deadlock waiting for that lock to be released.
    await db.rollback()

    try:
        await restore_backup(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "restored"}


# ── Media requests (approval queue) ──────────────────────────────────────────

@router.get("/requests/pending-count")
async def pending_requests_count(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(func.count()).select_from(MediaRequest).where(MediaRequest.status == RequestStatus.pending)
    )
    return {"pending": result.scalar_one()}


@router.get("/requests")
async def list_requests(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(MediaRequest, User)
        .join(User, User.id == MediaRequest.user_id)
        .order_by(MediaRequest.updated_at.desc())
    )
    rows = result.all()
    return [
        {
            "id":          req.id,
            "tmdb_id":     req.tmdb_id,
            "media_type":  req.media_type,
            "title":       req.title,
            "poster_path": req.poster_path,
            "status":      req.status.value,
            "reviewed_by": req.reviewed_by,
            "created_at":  req.created_at,
            "updated_at":  req.updated_at,
            "user": {
                "id":           user.id,
                "username":     user.username,
                "display_name": user.username,
            },
        }
        for req, user in rows
    ]


@router.post("/requests/{request_id}/approve")
async def approve_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    req_q = await db.execute(select(MediaRequest).where(MediaRequest.id == request_id))
    req = req_q.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    gs = await _get_or_create_global_settings(db)
    settings_q = await db.execute(select(UserSettings).where(UserSettings.user_id == req.user_id))
    settings = settings_q.scalar_one_or_none()

    if req.media_type == "movie":
        from routers.media import _effective_radarr
        from core import radarr as radarr_core
        radarr_cfg = _effective_radarr(settings, gs)
        if not radarr_cfg:
            raise HTTPException(status_code=400, detail="Radarr not configured")
        try:
            await radarr_core.add_movie(
                url=radarr_cfg.radarr_url,
                token=radarr_cfg.radarr_token,
                tmdb_id=req.tmdb_id,
                title=req.title or "",
                root_folder=radarr_cfg.radarr_root_folder,
                quality_profile_id=radarr_cfg.radarr_quality_profile,
                tags=radarr_cfg.radarr_tags,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Radarr error: {e}")

    elif req.media_type == "series":
        from routers.media import _effective_sonarr, get_user_tmdb_key
        from core import sonarr as sonarr_core, tmdb as tmdb_core
        sonarr_cfg = _effective_sonarr(settings, gs)
        if not sonarr_cfg:
            raise HTTPException(status_code=400, detail="Sonarr not configured")
        try:
            tmdb_key = await get_user_tmdb_key(db, current_user.id)
            ext_ids = await tmdb_core.get_external_ids(req.tmdb_id, "tv", api_key=tmdb_key)
            tvdb_id = ext_ids.get("tvdb_id")
            if not tvdb_id:
                raise HTTPException(status_code=400, detail="Could not find TVDB ID")
            await sonarr_core.add_series(
                url=sonarr_cfg.sonarr_url,
                token=sonarr_cfg.sonarr_token,
                tvdb_id=tvdb_id,
                root_folder=sonarr_cfg.sonarr_root_folder,
                quality_profile_id=sonarr_cfg.sonarr_quality_profile,
                tags=sonarr_cfg.sonarr_tags,
                season_folder=sonarr_cfg.sonarr_season_folder if sonarr_cfg.sonarr_season_folder is not None else True,
            )
        except Exception as e:
            if isinstance(e, HTTPException): raise e
            raise HTTPException(status_code=500, detail=f"Sonarr error: {e}")

    req.status = RequestStatus.approved
    req.reviewed_by = current_user.id
    req.updated_at = func.now()
    await db.commit()
    return {"status": "approved"}


@router.post("/requests/{request_id}/reject")
async def reject_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    req_q = await db.execute(select(MediaRequest).where(MediaRequest.id == request_id))
    req = req_q.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    req.status = RequestStatus.rejected
    req.reviewed_by = current_user.id
    req.updated_at = func.now()
    await db.commit()
    return {"status": "rejected"}
