"""add allow_public_profiles global setting

Revision ID: e4a1c2b3d5f6
Revises: d4e8f1a3c6b9
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa


revision = "e4a1c2b3d5f6"
down_revision = "d4e8f1a3c6b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "global_settings",
        sa.Column("allow_public_profiles", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("global_settings", "allow_public_profiles")
