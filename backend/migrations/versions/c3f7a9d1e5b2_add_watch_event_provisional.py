"""Add provisional to watch_events

Revision ID: c3f7a9d1e5b2
Revises: a1d4e7f9b2c3
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3f7a9d1e5b2'
down_revision = 'a1d4e7f9b2c3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('watch_events', sa.Column('provisional', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('watch_events', 'provisional')
