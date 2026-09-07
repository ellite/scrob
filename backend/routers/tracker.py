"""AniList-style status lists for all media types that Scrob does not sync."""

from datetime import datetime
from typing import Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db import get_db
from dependencies import get_current_user_or_api_key
from models.tracker import CatalogItem, TrackerEntry
from models.collection import Collection
from models.events import WatchEvent
from models.media import Media
from models.base import MediaType
from models.show import Show
from models.users import User

router = APIRouter()

TrackerMediaType = Literal["movie", "series", "anime", "manga", "book", "game", "comic", "boardgame"]
TrackerStatus = Literal["current", "completed", "planning", "paused", "dropped", "repeating"]


class CatalogItemInput(BaseModel):
    source: str = Field(min_length=1, max_length=32)
    external_id: Optional[str] = Field(default=None, max_length=255)
    media_type: TrackerMediaType
    title: str = Field(min_length=1, max_length=500)
    original_title: Optional[str] = Field(default=None, max_length=500)
    cover_url: Optional[HttpUrl] = None
    description: Optional[str] = None
    release_date: Optional[str] = Field(default=None, max_length=20)
    total_units: Optional[int] = Field(default=None, ge=0)
    metadata: Optional[dict] = None

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("external_id")
    @classmethod
    def normalize_external_id(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value else None


class TrackerEntryCreate(BaseModel):
    item: CatalogItemInput
    status: TrackerStatus = "planning"
    progress: int = Field(default=0, ge=0)
    score: Optional[float] = Field(default=None, ge=0, le=100)
    notes: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TrackerEntryUpdate(BaseModel):
    status: Optional[TrackerStatus] = None
    progress: Optional[int] = Field(default=None, ge=0)
    score: Optional[float] = Field(default=None, ge=0, le=100)
    notes: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


def _entry_payload(entry: TrackerEntry) -> dict:
    item = entry.item
    return {
        "id": entry.id,
        "status": entry.status,
        "progress": entry.progress,
        "score": entry.score,
        "notes": entry.notes,
        "started_at": entry.started_at.isoformat() if entry.started_at else None,
        "completed_at": entry.completed_at.isoformat() if entry.completed_at else None,
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
        "synced": False,
        "item": {
            "id": item.id,
            "source": item.source,
            "external_id": item.external_id,
            "media_type": item.media_type,
            "title": item.title,
            "original_title": item.original_title,
            "cover_url": item.cover_url,
            "description": item.description,
            "release_date": item.release_date,
            "total_units": item.total_units,
            "metadata": item.extra_data,
        },
    }


def _synced_entry_payload(media: Media, completed: bool, has_activity: bool, show: Show | None) -> dict:
    """Adapt Scrob's provider-backed movie/show to the tracker card shape.

    No sync data is copied into tracker tables: every list refresh reads the
    canonical records that Nuvio/Stremio updated.  A user can still create a
    richer tracker entry for the same TMDB item; that one takes precedence.
    """
    media_type = "series" if media.media_type == MediaType.series else "movie"
    return {
        "id": -media.id,
        "status": "completed" if completed else "current" if has_activity else "planning",
        "progress": 0,
        "score": None,
        "notes": None,
        "started_at": None,
        "completed_at": None,
        "created_at": media.created_at.isoformat(),
        "updated_at": media.updated_at.isoformat(),
        "synced": True,
        "item": {
            "id": media.id,
            "source": "tmdb" if media.tmdb_id else "scrob",
            "external_id": str(media.tmdb_id or media.id),
            "media_type": media_type,
            "title": media.title or (show.title if show else "Untitled"),
            "original_title": media.original_title,
            "cover_url": media.poster_path or (show.poster_path if show else None),
            "description": media.overview,
            "release_date": media.release_date or (show.first_air_date if show else None),
            "total_units": None,
            "metadata": None,
        },
    }


async def _entry_or_404(entry_id: int, user_id: int, db: AsyncSession) -> TrackerEntry:
    result = await db.execute(
        select(TrackerEntry)
        .options(selectinload(TrackerEntry.item))
        .where(TrackerEntry.id == entry_id, TrackerEntry.user_id == user_id)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Tracked item not found")
    return entry


@router.get("")
async def list_entries(
    media_type: Optional[TrackerMediaType] = Query(default=None),
    status_filter: Optional[TrackerStatus] = Query(default=None, alias="status"),
    q: Optional[str] = Query(default=None, max_length=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    query = (
        select(TrackerEntry)
        .join(CatalogItem)
        .options(selectinload(TrackerEntry.item))
        .where(TrackerEntry.user_id == current_user.id)
        .order_by(TrackerEntry.updated_at.desc(), CatalogItem.title.asc())
    )
    if media_type:
        query = query.where(CatalogItem.media_type == media_type)
    if q:
        query = query.where(func.lower(CatalogItem.title).contains(q.strip().lower()))
    entries = (await db.execute(query)).scalars().all()
    payloads = [_entry_payload(entry) for entry in entries]
    existing_tmdb_keys = {
        (payload["item"]["external_id"], payload["item"]["media_type"])
        for payload in payloads if payload["item"]["source"] == "tmdb"
    }

    # Bring provider-backed movies and series into the same focused view.
    # These are virtual entries rather than duplicate tracker rows, so an
    # inbound Nuvio/Stremio pull is immediately visible here and continues to
    # own its playback state.
    synced_query = (
        select(Media)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.media_type.in_([MediaType.movie, MediaType.series]),
        )
        .distinct()
    )
    synced_media = (await db.execute(synced_query)).scalars().all()
    if synced_media:
        media_ids = [media.id for media in synced_media]
        activity_rows = await db.execute(
            select(
                WatchEvent.media_id,
                func.max(case((WatchEvent.completed.is_(True), 1), else_=0)),
                func.count(WatchEvent.id),
            )
            .where(WatchEvent.user_id == current_user.id, WatchEvent.media_id.in_(media_ids))
            .group_by(WatchEvent.media_id)
        )
        activity_by_id = {row[0]: (bool(row[1]), bool(row[2])) for row in activity_rows.all()}
        series_ids = [media.tmdb_id for media in synced_media if media.media_type == MediaType.series and media.tmdb_id]
        shows_by_tmdb: dict[int, Show] = {}
        if series_ids:
            shows = await db.execute(select(Show).where(Show.tmdb_id.in_(series_ids)))
            shows_by_tmdb = {show.tmdb_id: show for show in shows.scalars().all() if show.tmdb_id}
        for media in synced_media:
            normalized_type = "series" if media.media_type == MediaType.series else "movie"
            identity = (str(media.tmdb_id), normalized_type)
            if media.tmdb_id and identity in existing_tmdb_keys:
                continue
            if media_type and normalized_type != media_type:
                continue
            if q and q.strip().lower() not in media.title.lower():
                continue
            completed, has_activity = activity_by_id.get(media.id, (False, False))
            payload = _synced_entry_payload(media, completed, has_activity, shows_by_tmdb.get(media.tmdb_id))
            payloads.append(payload)

    counts = {key: 0 for key in ("current", "completed", "planning", "paused", "dropped", "repeating")}
    for payload in payloads:
        counts[payload["status"]] += 1
    if status_filter:
        payloads = [payload for payload in payloads if payload["status"] == status_filter]
    return {"entries": payloads, "counts": counts}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_entry(
    body: TrackerEntryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    item_input = body.item
    external_id = item_input.external_id or f"manual:{uuid4()}"
    item_result = await db.execute(
        select(CatalogItem).where(
            CatalogItem.source == item_input.source,
            CatalogItem.external_id == external_id,
            CatalogItem.media_type == item_input.media_type,
        )
    )
    item = item_result.scalar_one_or_none()
    if not item:
        item = CatalogItem(
            source=item_input.source,
            external_id=external_id,
            media_type=item_input.media_type,
            title=item_input.title,
            original_title=item_input.original_title,
            cover_url=str(item_input.cover_url) if item_input.cover_url else None,
            description=item_input.description,
            release_date=item_input.release_date,
            total_units=item_input.total_units,
            extra_data=item_input.metadata,
        )
        db.add(item)
        await db.flush()

    existing = await db.execute(
        select(TrackerEntry.id).where(TrackerEntry.user_id == current_user.id, TrackerEntry.item_id == item.id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="This item is already in your tracker")

    entry = TrackerEntry(
        user_id=current_user.id,
        item_id=item.id,
        status=body.status,
        progress=body.progress,
        score=body.score,
        notes=body.notes,
        started_at=body.started_at,
        completed_at=body.completed_at,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry, attribute_names=["item"])
    return _entry_payload(entry)


@router.patch("/{entry_id}")
async def update_entry(
    entry_id: int,
    body: TrackerEntryUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    entry = await _entry_or_404(entry_id, current_user.id, db)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(entry, field, value)
    if changes.get("status") == "completed" and entry.completed_at is None:
        entry.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(entry, attribute_names=["item"])
    return _entry_payload(entry)


@router.delete("/{entry_id}")
async def delete_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    entry = await _entry_or_404(entry_id, current_user.id, db)
    await db.delete(entry)
    await db.commit()
    return {"message": "Tracked item removed"}
