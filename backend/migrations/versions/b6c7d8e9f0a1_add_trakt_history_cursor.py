"""add Trakt history cursor

Revision ID: b6c7d8e9f0a1
Revises: a9b8c7d6e5f4
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa


revision = "b6c7d8e9f0a1"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("trakt_history_cursor_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "trakt_history_cursor_at")
