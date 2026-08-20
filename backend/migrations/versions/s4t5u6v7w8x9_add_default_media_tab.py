"""add default media tab preference

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


revision = "s4t5u6v7w8x9"
down_revision = "r3s4t5u6v7w8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("default_media_tab", sa.String(length=20), nullable=False, server_default="explore"),
    )
    op.create_check_constraint(
        "ck_user_settings_default_media_tab",
        "user_settings",
        "default_media_tab IN ('explore', 'collection')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_user_settings_default_media_tab", "user_settings", type_="check")
    op.drop_column("user_settings", "default_media_tab")
