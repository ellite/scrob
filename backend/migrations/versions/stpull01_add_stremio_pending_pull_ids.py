"""add connections.stremio_pending_pull_ids

Revision ID: stpull01
Revises: wesrc477
Create Date: 2026-10-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "stpull01"
down_revision = "scrobpct"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_server_connections",
        sa.Column("stremio_pending_pull_ids", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_server_connections", "stremio_pending_pull_ids")
