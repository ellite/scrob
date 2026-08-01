"""add Stremio provider

Revision ID: d7e8f9a0b1c2
Revises: c7d8e9f0a1b2
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "d7e8f9a0b1c2"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE collectionsource ADD VALUE IF NOT EXISTS 'stremio'")
    op.drop_constraint("ck_msc_type", "media_server_connections", type_="check")
    op.create_check_constraint(
        "ck_msc_type",
        "media_server_connections",
        "type IN ('plex', 'jellyfin', 'emby', 'nuvio', 'stremio')",
    )
    op.add_column(
        "media_server_connections",
        sa.Column("stremio_pull_cursor_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "media_server_connections",
        sa.Column(
            "stremio_full_sync_done",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "media_server_connections",
        sa.Column("stremio_pushed_library_ids", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_server_connections", "stremio_pushed_library_ids")
    op.drop_column("media_server_connections", "stremio_full_sync_done")
    op.drop_column("media_server_connections", "stremio_pull_cursor_at")
    # PostgreSQL enum values cannot be removed safely in-place. Keep the value and
    # compatible constraint; older application versions do not create Stremio rows.
