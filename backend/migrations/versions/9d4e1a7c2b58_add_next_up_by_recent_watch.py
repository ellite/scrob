"""Add next_up_by_recent_watch to user_settings

Revision ID: 9d4e1a7c2b58
Revises: stpull01
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = '9d4e1a7c2b58'
down_revision = 'stpull01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('next_up_by_recent_watch', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('user_settings', 'next_up_by_recent_watch')
