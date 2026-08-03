"""add plex history cursor

Revision ID: f8c2c34badd4
Revises: d4adcaccbc4f
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f8c2c34badd4"
down_revision = "d4adcaccbc4f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_server_connections",
        sa.Column("plex_history_cursor_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_server_connections", "plex_history_cursor_at")
