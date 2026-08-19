"""add enable_logged_out_navigation global setting

Revision ID: 7786946fb622
Revises: b2c3d4e5f6a8
Create Date: 2026-08-18
"""

from alembic import op
import sqlalchemy as sa


revision = "7786946fb622"
down_revision = "b2c3d4e5f6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "global_settings",
        sa.Column("enable_logged_out_navigation", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("global_settings", "enable_logged_out_navigation")
