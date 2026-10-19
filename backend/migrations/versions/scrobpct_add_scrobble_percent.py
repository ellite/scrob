"""add per-connection scrobble percent

Revision ID: scrobpct
Revises: wesrc477
Create Date: 2026-10-19
"""

from alembic import op
import sqlalchemy as sa


revision = "scrobpct"
down_revision = "wesrc477"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("media_server_connections", "scrobble_connections"):
        op.add_column(
            table,
            sa.Column("scrobble_percent", sa.Integer(), nullable=False, server_default="75"),
        )


def downgrade() -> None:
    for table in ("media_server_connections", "scrobble_connections"):
        op.drop_column(table, "scrobble_percent")
