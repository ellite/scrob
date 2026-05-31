"""Add use_hls_player to user_settings

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision = 'n8o9p0q1r2s3'
down_revision = '11f34ec77054'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('use_hls_player', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('user_settings', 'use_hls_player')
