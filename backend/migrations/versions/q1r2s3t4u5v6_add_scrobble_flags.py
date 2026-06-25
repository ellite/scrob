"""Add trakt_scrobble and simkl_scrobble flags to user_settings

Revision ID: q1r2s3t4u5v6
Revises: p0q1r2s3t4u5
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'q1r2s3t4u5v6'
down_revision = 'p0q1r2s3t4u5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('trakt_scrobble', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('user_settings', sa.Column('simkl_scrobble', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('user_settings', 'simkl_scrobble')
    op.drop_column('user_settings', 'trakt_scrobble')
