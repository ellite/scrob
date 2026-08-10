"""Add plex_watchlist_synced_keys baseline to media_server_connections

Revision ID: 5806acaa7d6e
Revises: e4a1c2b3d5f6
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "5806acaa7d6e"
down_revision = "e4a1c2b3d5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_server_connections",
        sa.Column("plex_watchlist_synced_keys", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_server_connections", "plex_watchlist_synced_keys")
