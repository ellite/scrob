"""add outbound playback progress flag

Revision ID: u5v6w7x8y9z0
Revises: t4u5v6w7x8y9
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "u5v6w7x8y9z0"
down_revision = "t4u5v6w7x8y9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_server_connections",
        sa.Column("push_playback", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("media_server_connections", "push_playback")
