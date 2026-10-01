"""add default_episode_order to global_settings and user_settings

The user column is nullable on purpose: NULL means "inherit the server-wide
default", the same override chain the TVDB API key already uses.

Revision ID: eodefault
Revises: cmttvdb446
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa


revision = "eodefault"
down_revision = "cmttvdb446"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "global_settings",
        sa.Column("default_episode_order", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "user_settings",
        sa.Column("default_episode_order", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "default_episode_order")
    op.drop_column("global_settings", "default_episode_order")
