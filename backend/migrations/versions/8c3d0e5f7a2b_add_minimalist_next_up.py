"""Add minimalist_next_up to user_settings

Revision ID: 8c3d0e5f7a2b
Revises: 7b2c9d4e6f1a
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa

revision = '8c3d0e5f7a2b'
down_revision = '7b2c9d4e6f1a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('minimalist_next_up', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('user_settings', 'minimalist_next_up')
