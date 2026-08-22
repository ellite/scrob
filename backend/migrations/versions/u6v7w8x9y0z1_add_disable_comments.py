"""add disable_comments global setting

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa


revision = "u6v7w8x9y0z1"
down_revision = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "global_settings",
        sa.Column("disable_comments", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("global_settings", "disable_comments")
