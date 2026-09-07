"""Read Yamtrack's non-playback media records for the native tracker."""

import csv
import io
from dataclasses import dataclass
from datetime import datetime

from dateutil.parser import isoparse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.scrob_import import MAX_TOTAL_SIZE
from models.tracker import CatalogItem, TrackerEntry

_REQUIRED_COLUMNS = {"media_id", "source", "media_type", "title"}
_TYPE_MAP = {
    "movie": "movie", "tv": "series", "series": "series", "anime": "anime",
    "manga": "manga", "book": "book", "game": "game", "comic": "comic",
    "boardgame": "boardgame",
}
_STATUS_MAP = {
    "in progress": "current", "current": "current", "completed": "completed",
    "planning": "planning", "paused": "paused", "dropped": "dropped",
    "repeating": "repeating",
}


@dataclass(frozen=True)
class YamtrackTrackerRecord:
    source: str
    external_id: str
    media_type: str
    title: str
    cover_url: str | None
    status: str
    progress: int
    score: float | None
    notes: str | None
    started_at: datetime | None
    completed_at: datetime | None


def _text(value: str | None) -> str:
    return (value or "").strip()


def _parse_int(value: str | None) -> int:
    try:
        return max(0, int(float(_text(value))))
    except (ValueError, OverflowError):
        return 0


def _parse_score(value: str | None) -> float | None:
    try:
        parsed = float(_text(value))
        return parsed if 0 <= parsed <= 100 else None
    except ValueError:
        return None


def _parse_date(value: str | None) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        return isoparse(raw).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return None


def parse_yamtrack_tracker_csv(content: bytes) -> list[YamtrackTrackerRecord]:
    """Return media rows that have a direct equivalent in the tracker.

    Episodes and seasons remain with Scrob's playback importer; their state is
    not a single AniList-style entry.  Rows from a Floppy export are handled
    too, with its ``row_type`` defaulting exactly as Yamtrack does.
    """
    if len(content) > MAX_TOTAL_SIZE:
        raise ValueError("Export file is too large to import.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("This doesn't look like a valid CSV file (not UTF-8 text).") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not _REQUIRED_COLUMNS.issubset(set(reader.fieldnames or [])):
        raise ValueError("This doesn't look like a Yamtrack or Floppy export.")

    records: list[YamtrackTrackerRecord] = []
    try:
        for row in reader:
            if _text(row.get("row_type")).lower() not in ("", "media"):
                continue
            media_type = _TYPE_MAP.get(_text(row.get("media_type")).lower())
            source = _text(row.get("source")).lower()
            external_id = _text(row.get("media_id"))
            title = _text(row.get("title"))
            if not media_type or not source or not external_id or not title:
                continue
            if len(source) > 32 or len(external_id) > 255 or len(title) > 500:
                continue
            score = _parse_score(row.get("score"))
            records.append(YamtrackTrackerRecord(
                source=source,
                external_id=external_id,
                media_type=media_type,
                title=title,
                cover_url=_text(row.get("image"))[:1000] or None,
                status=_STATUS_MAP.get(_text(row.get("status")).lower(), "planning"),
                progress=_parse_int(row.get("progress")),
                score=score,
                notes=_text(row.get("notes")) or None,
                started_at=_parse_date(row.get("start_date")),
                completed_at=_parse_date(row.get("end_date")),
            ))
    except csv.Error as exc:
        raise ValueError(f"This CSV file is malformed and couldn't be read: {exc}") from exc
    return records


async def apply_yamtrack_tracker_import(
    db: AsyncSession, user_id: int, records: list[YamtrackTrackerRecord]
) -> dict[str, int]:
    """Upsert imported records, keeping provider identity canonical."""
    added = updated = 0
    for record in records:
        item_result = await db.execute(select(CatalogItem).where(
            CatalogItem.source == record.source,
            CatalogItem.external_id == record.external_id,
            CatalogItem.media_type == record.media_type,
        ))
        item = item_result.scalar_one_or_none()
        if item is None:
            item = CatalogItem(
                source=record.source, external_id=record.external_id,
                media_type=record.media_type, title=record.title,
                cover_url=record.cover_url,
            )
            db.add(item)
            await db.flush()
        else:
            item.title = record.title
            item.cover_url = record.cover_url or item.cover_url

        entry_result = await db.execute(select(TrackerEntry).where(
            TrackerEntry.user_id == user_id, TrackerEntry.item_id == item.id,
        ))
        entry = entry_result.scalar_one_or_none()
        if entry is None:
            db.add(TrackerEntry(
                user_id=user_id, item_id=item.id, status=record.status,
                progress=record.progress, score=record.score, notes=record.notes,
                started_at=record.started_at, completed_at=record.completed_at,
            ))
            added += 1
        else:
            entry.status = record.status
            entry.progress = record.progress
            entry.score = record.score
            entry.notes = record.notes
            entry.started_at = record.started_at
            entry.completed_at = record.completed_at
            updated += 1
    return {"added": added, "updated": updated}
