"""Backfill collection_files.connection_id rows left NULL by the scrobble
webhook handlers hardcoding connection_id=None (#299)

Re-runs the same unambiguous-single-connection backfill 0005_cf_connection_id
did when the column was first added, since these NULL rows kept accumulating
after that one-time backfill ran - the webhook code bug is fixed separately,
this only catches up existing data.

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-08-22
"""
from alembic import op
import sqlalchemy as sa

revision = 't5u6v7w8x9y0'
down_revision = 's4t5u6v7w8x9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for src_type in ('plex', 'jellyfin', 'emby'):
        bind.execute(sa.text(f"""
            UPDATE collection_files cf
            SET connection_id = msc.id
            FROM collections c
            JOIN (
                SELECT DISTINCT ON (user_id) id, user_id
                FROM media_server_connections
                WHERE type = '{src_type}'
                ORDER BY user_id, id ASC
            ) msc ON msc.user_id = c.user_id
            WHERE cf.collection_id = c.id
              AND cf.source = '{src_type}'
              AND cf.connection_id IS NULL
              AND (
                SELECT COUNT(*) FROM media_server_connections
                WHERE user_id = c.user_id AND type = '{src_type}'
              ) = 1
        """))


def downgrade() -> None:
    # Data backfill only - no schema change, and there's no record of which
    # rows this touched vs. were already correct, so there's nothing safe to
    # revert.
    pass
