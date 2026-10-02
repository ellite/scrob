"""Per-connection memory of what a push last sent (#421, #422).

A scheduled push used to re-send the user's whole rating/watched snapshot and
repeat a server lookup for every item it couldn't place, every run. That
rate-limits Nuvio and piles write load onto Plex. PushState lets the push skip
items whose last pushed value is unchanged, and skip lookups that recently
came up empty.

The trade-off is that a skipped item is no longer re-asserted, so a value
someone edited directly on the server stays put until the next full
reconcile. A manual push, and a scheduled one when the last full reconcile is
older than FULL_RECONCILE_INTERVAL, ignore the stored state and push
everything (refreshing the state as they go). A scheduled full reconcile that
follows an interrupted one (a 429 half way through, say) resumes from it
rather than starting over, so it can't fail the same way forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.connection_push_state import ConnectionPushState
from models.sync import SyncJob, SyncStatus

# How long a scheduled push trusts its state before reconciling everything.
FULL_RECONCILE_INTERVAL = timedelta(days=7)
# A lookup that found nothing is not retried for this long: the item is
# probably just not on that server, and each retry is a request per item.
MISS_TTL = timedelta(hours=24)

MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"

RATING = "rating"
WATCHED = "watched"
MISS = "miss"


def season_rating_key(season_number: int) -> str:
    return f"season_rating:{season_number}"


def season_miss_key(season_number: int) -> str:
    return f"miss:season:{season_number}"


# 4 bound columns per row, and asyncpg allows 32767 parameters per statement.
_UPSERT_CHUNK = 4000
_DELETE_CHUNK = 5000
_EPSILON = 1e-6


@dataclass(frozen=True)
class PushPlan:
    mode: str
    # Trust the stored state and skip unchanged items.
    use_state: bool
    # For a full run resuming an interrupted one: state written at or after
    # this moment came from that run and is trusted.
    resume_since: datetime | None = None


async def plan_push(
    db: AsyncSession, connection_id: int, *, incremental: bool, now: datetime | None = None
) -> PushPlan:
    """Decide how a push should run. A manual push (incremental=False) always
    reconciles everything. A scheduled one is incremental unless no full push
    completed within FULL_RECONCILE_INTERVAL."""
    if not incremental:
        return PushPlan(MODE_FULL, use_state=False)
    now = now or datetime.utcnow()
    last_full = await db.scalar(
        select(func.max(SyncJob.updated_at)).where(
            SyncJob.connection_id == connection_id,
            SyncJob.job_type == "push",
            SyncJob.status == SyncStatus.completed,
            SyncJob.stats["mode"].as_string() == MODE_FULL,
        )
    )
    if last_full is not None and now - last_full < FULL_RECONCILE_INTERVAL:
        return PushPlan(MODE_INCREMENTAL, use_state=True)
    # Due for a full reconcile. If earlier attempts since the last completed
    # one were interrupted, pick up from the first of them.
    window_start = max(last_full or datetime.min, now - FULL_RECONCILE_INTERVAL)
    resume_since = await db.scalar(
        select(func.min(SyncJob.created_at)).where(
            SyncJob.connection_id == connection_id,
            SyncJob.job_type == "push",
            SyncJob.status.in_([SyncStatus.failed, SyncStatus.cancelled]),
            SyncJob.created_at > window_start,
        )
    )
    return PushPlan(MODE_FULL, use_state=False, resume_since=resume_since)


class PushState:
    """One connection's push state for the duration of a push.

    A plan with use_state False means "ignore what was stored": every check
    answers as if nothing was ever pushed (bar what a resumed run already
    pushed), but new results are still recorded so the next incremental run
    has something to compare against.
    """

    def __init__(
        self,
        rows: dict[tuple[int, str], tuple[float | None, datetime]] | None = None,
        *,
        use_state: bool = True,
        resume_since: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        self._rows = rows or {}
        self.use_state = use_state
        self.resume_since = resume_since
        self._now = now or datetime.utcnow()
        self._updates: dict[tuple[int, str], float | None] = {}
        self._forgotten: set[tuple[int, str]] = set()
        self._failed: set[tuple[int, str]] = set()
        self.skipped = 0

    @classmethod
    async def load(cls, db: AsyncSession, connection_id: int, plan: PushPlan) -> "PushState":
        if not plan.use_state and plan.resume_since is None:
            return cls(use_state=False)
        result = await db.execute(
            select(
                ConnectionPushState.media_id,
                ConnectionPushState.item_key,
                ConnectionPushState.value,
                ConnectionPushState.updated_at,
            ).where(ConnectionPushState.connection_id == connection_id)
        )
        rows = {(m, k): (v, at) for m, k, v, at in result.all()}
        return cls(rows, use_state=plan.use_state, resume_since=plan.resume_since)

    # ── reads ──────────────────────────────────────────────────────────────

    def unchanged(self, media_id: int, key: str, value: float | None) -> bool:
        """True when this exact value was already pushed. Counts a skip."""
        row = self._rows.get((media_id, key))
        if row is None or row[0] is None or value is None:
            return False
        if not self.use_state and not (self.resume_since and row[1] >= self.resume_since):
            return False
        if abs(row[0] - value) >= _EPSILON:
            return False
        self.skipped += 1
        return True

    def recent_miss(self, media_id: int, key: str = MISS) -> bool:
        """True when a lookup for this item came up empty within MISS_TTL."""
        if not self.use_state:
            return False
        row = self._rows.get((media_id, key))
        return row is not None and self._now - row[1] < MISS_TTL

    # ── writes (applied by save()) ─────────────────────────────────────────

    def record(self, media_id: int, key: str, value: float | None) -> None:
        self._updates[(media_id, key)] = value

    def record_miss(self, media_id: int, key: str = MISS) -> None:
        self._updates[(media_id, key)] = None

    def forget(self, media_id: int, key: str) -> None:
        """Drop a stored row (a lookup that used to miss now succeeds)."""
        self._forgotten.add((media_id, key))

    def fail(self, media_id: int, key: str) -> None:
        """A push for this item failed: never record it as pushed, even if
        another push for the same item (a second server-side copy) worked."""
        self._failed.add((media_id, key))

    # ── persistence ────────────────────────────────────────────────────────

    def pending(self) -> dict[tuple[int, str], float | None]:
        return {
            item: value
            for item, value in self._updates.items()
            if item not in self._failed or item[1].startswith(MISS)
        }

    async def save(self, db: AsyncSession, connection_id: int) -> None:
        pending = self.pending()
        forgotten = {item for item in self._forgotten if item not in pending}
        if forgotten:
            by_key: dict[str, list[int]] = {}
            for media_id, key in forgotten:
                by_key.setdefault(key, []).append(media_id)
            for key, media_ids in by_key.items():
                for i in range(0, len(media_ids), _DELETE_CHUNK):
                    await db.execute(
                        delete(ConnectionPushState).where(
                            ConnectionPushState.connection_id == connection_id,
                            ConnectionPushState.item_key == key,
                            ConnectionPushState.media_id.in_(media_ids[i : i + _DELETE_CHUNK]),
                        )
                    )
        rows = [
            {
                "connection_id": connection_id,
                "media_id": media_id,
                "item_key": key,
                "value": value,
                "updated_at": self._now,
            }
            for (media_id, key), value in pending.items()
        ]
        for i in range(0, len(rows), _UPSERT_CHUNK):
            # Postgres in production; the test suite runs the same code on SQLite.
            insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
            stmt = insert(ConnectionPushState).values(rows[i : i + _UPSERT_CHUNK])
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["connection_id", "media_id", "item_key"],
                    set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
                )
            )
        await db.commit()
        # Saved: a second save (the job's error handler runs one too) is a no-op.
        self._updates.clear()
        self._forgotten.clear()
