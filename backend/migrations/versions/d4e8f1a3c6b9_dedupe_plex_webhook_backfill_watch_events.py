"""Dedupe Plex webhook/backfill watch events (GitHub #135)

One-time data cleanup: before this fix, the real-time Plex webhook and the
Plex history backfill (routers/sync.py:_backfill_plex_watch_history) could
each independently record the same physical play as its own WatchEvent, since
their watched_at values rarely matched exactly (see GitHub #135 for the root
cause). This finds and removes the resulting duplicates using the same
merge rule the code now enforces going forward — see
core/watch_event_cleanup.py for the exact matching logic and its tests.

Revision ID: d4e8f1a3c6b9
Revises: c3f7a9d1e5b2
Create Date: 2026-08-04
"""
import os
import sys

import sqlalchemy as sa
from alembic import op

# Migrations run with backend/ on sys.path already (see migrations/env.py),
# but that insert doesn't happen for every invocation path — make sure it's
# there before importing app code.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from core.watch_event_cleanup import WatchEventRow, find_duplicate_watch_event_ids

revision = 'd4e8f1a3c6b9'
down_revision = 'c3f7a9d1e5b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Only media collected via Plex for that user could plausibly have both a
    # webhook-created and a backfill-created WatchEvent — this is what keeps
    # the cleanup from ever considering Trakt/Simkl/MDBList-only history,
    # which shares the backfill row's "no progress data" signature but isn't
    # this bug.
    rows = bind.execute(sa.text("""
        SELECT DISTINCT we.id, we.user_id, we.media_id, we.watched_at,
               we.progress_percent, we.progress_seconds
        FROM watch_events we
        JOIN collections c ON c.user_id = we.user_id AND c.media_id = we.media_id
        JOIN collection_files cf ON cf.collection_id = c.id AND cf.source = 'plex'
        WHERE we.completed = true AND we.watched_at IS NOT NULL
    """)).fetchall()

    candidates = [
        WatchEventRow(
            id=row.id, user_id=row.user_id, media_id=row.media_id, watched_at=row.watched_at,
            progress_percent=row.progress_percent, progress_seconds=row.progress_seconds,
        )
        for row in rows
    ]
    to_delete = find_duplicate_watch_event_ids(candidates)

    if to_delete:
        # RewatchProgress.watch_event_id is ON DELETE CASCADE, so no orphaned
        # rewatch-progress rows are left behind.
        for i in range(0, len(to_delete), 1000):
            chunk = to_delete[i:i + 1000]
            bind.execute(sa.text("DELETE FROM watch_events WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ), {"ids": chunk})

    print(f"  #135 cleanup: removed {len(to_delete)} duplicate Plex watch event(s) out of {len(candidates)} candidate(s) checked.")


def downgrade() -> None:
    # Deleted rows aren't recoverable — this cleanup has no downgrade path.
    pass
