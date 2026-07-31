from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from dependencies import get_current_user
from models.users import User, UserSettings
from core.data_export import build_export_zip

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
