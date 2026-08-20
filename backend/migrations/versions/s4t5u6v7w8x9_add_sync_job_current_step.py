"""add current_step to sync_jobs

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = 's4t5u6v7w8x9'
down_revision = 'r3s4t5u6v7w8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('sync_jobs', sa.Column('current_step', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('sync_jobs', 'current_step')
