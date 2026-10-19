"""add simkl_sync_dropped / simkl_push_dropped (#474)

Revision ID: simkldrop
Revises: oidchard
Create Date: 2026-10-14
"""

from alembic import op
import sqlalchemy as sa


revision = "simkldrop"
down_revision = "oidchard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("simkl_sync_dropped", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_settings", sa.Column("simkl_push_dropped", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("user_settings", "simkl_push_dropped")
    op.drop_column("user_settings", "simkl_sync_dropped")
