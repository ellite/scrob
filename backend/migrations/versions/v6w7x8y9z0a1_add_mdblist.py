"""add MDBList synchronization settings

Revision ID: v6w7x8y9z0a1
Revises: u5v6w7x8y9z0
Create Date: 2026-07-17
"""

from alembic import op
import sqlalchemy as sa


revision = "v6w7x8y9z0a1"
down_revision = "u5v6w7x8y9z0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("mdblist_api_key", sa.String(255), nullable=True))
    op.add_column(
        "user_settings",
        sa.Column("mdblist_sync_watched", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "user_settings",
        sa.Column("mdblist_sync_ratings", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "user_settings",
        sa.Column("mdblist_sync_watchlist", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "user_settings",
        sa.Column("mdblist_push_watched", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "user_settings",
        sa.Column("mdblist_push_ratings", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "user_settings",
        sa.Column("mdblist_push_watchlist", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("lists", sa.Column("mdblist_slug", sa.String(255), nullable=True))
    op.execute("ALTER TYPE collectionsource ADD VALUE IF NOT EXISTS 'mdblist'")


def downgrade() -> None:
    op.drop_column("lists", "mdblist_slug")
    op.drop_column("user_settings", "mdblist_push_watchlist")
    op.drop_column("user_settings", "mdblist_push_ratings")
    op.drop_column("user_settings", "mdblist_push_watched")
    op.drop_column("user_settings", "mdblist_sync_watchlist")
    op.drop_column("user_settings", "mdblist_sync_ratings")
    op.drop_column("user_settings", "mdblist_sync_watched")
    op.drop_column("user_settings", "mdblist_api_key")
