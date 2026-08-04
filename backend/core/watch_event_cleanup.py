"""Pure logic for identifying Plex webhook/backfill duplicate WatchEvent pairs
(see GitHub #135) so they can be merged. Used by the one-time data migration
that repairs installs affected before the live-sync fix in routers/sync.py
(_backfill_plex_watch_history, PLEX_WEBHOOK_RECONCILE_WINDOW) shipped.

Kept dependency-free (no DB/ORM imports) so the merge decision itself is
directly unit-testable — the caller is responsible for fetching rows already
scoped to Plex-collected media (see the migration) and for the actual DELETE.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# Mirrors routers.sync.PLEX_WEBHOOK_RECONCILE_WINDOW: how long a webhook's
# receipt-time estimate can plausibly lag Plex's own authoritative viewedAt
# for the same play. Kept as a separate constant (rather than importing
# routers.sync here) so this module stays free of FastAPI/DB-engine imports —
# a data migration should not have to load the whole router stack.
RECONCILE_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class WatchEventRow:
    id: int
    user_id: int
    media_id: int
    watched_at: datetime
    progress_percent: float | None
    progress_seconds: int | None


def _is_webhook_signature(row: WatchEventRow) -> bool:
    # Across every WatchEvent creation site in the codebase, only
    # routers/webhooks.py:_write_watch_event ever sets progress_seconds
    # alongside progress_percent=1.0 — a reliable fingerprint even on rows
    # created before the `provisional` column existed to mark this directly.
    return row.progress_percent == 1.0 and row.progress_seconds is not None


def _is_backfill_signature(row: WatchEventRow) -> bool:
    # _backfill_plex_watch_history never sets either progress field. So,
    # identically, does every import path (Trakt/Simkl/MDBList/scrob-import) —
    # this signature alone doesn't prove Plex origin, which is why the caller
    # must pre-scope rows to Plex-collected media before calling this.
    return row.progress_percent is None and row.progress_seconds is None


def find_duplicate_watch_event_ids(rows: list[WatchEventRow]) -> list[int]:
    """Given every completed WatchEvent for media the caller has already
    scoped to a Plex collection (so this never considers Trakt/Simkl-only
    history), return the ids of rows that are the Plex-backfill "echo" of a
    webhook-recorded play and safe to delete.

    Grouped strictly per (user_id, media_id) — two different users watching
    the same item at the same moment are unrelated and never compared, and
    a user's own unrelated watches of two different items never interact.

    Within a group, rows are chain-clustered by consecutive time gaps (so a
    genuinely distinct rewatch outside the window starts a new cluster).
    A cluster is only ever resolved when it has exactly two rows and their
    signatures are exactly one webhook row + one backfill row — the backfill
    row (the one without progress data) is deleted, the webhook row is kept.
    Anything else — a lone row, three or more rows clustered together, two
    rows sharing the same signature, or a row matching neither known
    signature — is left untouched rather than guessed at.
    """
    by_group: dict[tuple[int, int], list[WatchEventRow]] = {}
    for row in rows:
        by_group.setdefault((row.user_id, row.media_id), []).append(row)

    to_delete: list[int] = []
    for group_rows in by_group.values():
        ordered = sorted(group_rows, key=lambda r: r.watched_at)
        cluster: list[WatchEventRow] = []
        for row in ordered:
            if cluster and (row.watched_at - cluster[-1].watched_at) > RECONCILE_WINDOW:
                to_delete.extend(_resolve_cluster(cluster))
                cluster = []
            cluster.append(row)
        if cluster:
            to_delete.extend(_resolve_cluster(cluster))

    return to_delete


def _resolve_cluster(cluster: list[WatchEventRow]) -> list[int]:
    if len(cluster) != 2:
        return []
    a, b = cluster
    if _is_webhook_signature(a) and _is_backfill_signature(b):
        return [b.id]
    if _is_webhook_signature(b) and _is_backfill_signature(a):
        return [a.id]
    return []
