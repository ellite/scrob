"""add Nuvio collection push and auto-push interval

Revision ID: z0a1b2c3d4e5
Revises: x8y9z0a1b2c3
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa


revision = "z0a1b2c3d4e5"
down_revision = "x8y9z0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_server_connections",
        sa.Column("push_collection", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "media_server_connections",
        sa.Column("auto_push_interval", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_server_connections", "auto_push_interval")
    op.drop_column("media_server_connections", "push_collection")
