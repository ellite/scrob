"""Add hide_watched_from_recently_added to user_settings

Revision ID: a1d4e7f9b2c3
Revises: f8c2c34badd4
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1d4e7f9b2c3'
down_revision = 'f8c2c34badd4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('hide_watched_from_recently_added', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('user_settings', 'hide_watched_from_recently_added')
