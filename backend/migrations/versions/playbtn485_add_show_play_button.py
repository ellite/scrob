"""add user_settings.show_play_button (#485)

Revision ID: playbtn485
Revises: simkldrop
Create Date: 2026-10-18
"""

from alembic import op
import sqlalchemy as sa


revision = "playbtn485"
down_revision = "simkldrop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("show_play_button", sa.Boolean(), nullable=False, server_default="true"))


def downgrade() -> None:
    op.drop_column("user_settings", "show_play_button")
