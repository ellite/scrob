"""add outbound collection push flags for trakt and mdblist

Revision ID: w7x8y9z0a1b2
Revises: v6w7x8y9z0a1
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa


revision = "w7x8y9z0a1b2"
down_revision = "v6w7x8y9z0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("trakt_push_collection", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "user_settings",
        sa.Column("mdblist_push_collection", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "mdblist_push_collection")
    op.drop_column("user_settings", "trakt_push_collection")
