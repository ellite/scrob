"""wetrakr list push bookmark

Revision ID: wetrakrlists
Revises: wetrakrsrc
Create Date: 2026-10-11

wetrakr_last_lists_push_at: list pushes get their own incremental bookmark,
separate from wetrakr_last_push_at, so one failing list or comment never
makes the next run resend the whole watched/ratings history.
"""
from alembic import op
import sqlalchemy as sa


revision = "wetrakrlists"
down_revision = "wetrakrsrc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("wetrakr_last_lists_push_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "wetrakr_last_lists_push_at")
