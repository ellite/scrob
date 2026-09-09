"""Backfill Media.runtime from tmdb_data.runtime (GitHub #383)

067d1ca (#169, 2026-08-13) started populating the top-level Media.runtime
column so the Now Playing bar's live progress interpolation can engage - but
only for rows enriched from then on. Enrichment isn't re-run for an
already-enriched item, so a library imported earlier keeps runtime NULL
forever while the value sits in tmdb_data.runtime. get_now_playing reads the
column directly, so those items' bar freezes between polls.

This copies the cached tmdb_data.runtime into the column for every row still
missing it. get_now_playing also gains a tmdb_data fallback and the
Jellyfin/Emby webhook now backfills the column itself (like Plex already
does), so this only ever needs to run once.

Revision ID: runtimebackfill
Revises: we355created
Create Date: 2026-09-09
"""

from alembic import op

revision = "runtimebackfill"
down_revision = "we355created"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE media
        SET runtime = (tmdb_data->>'runtime')::int
        WHERE runtime IS NULL
          AND tmdb_data->>'runtime' ~ '^[0-9]+$'
          AND (tmdb_data->>'runtime')::int > 0
        """
    )


def downgrade() -> None:
    # A one-off data backfill - the column just stays populated. Nothing to undo.
    pass
