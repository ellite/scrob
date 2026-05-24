"""add_next_up_hidden_shows

Revision ID: 938835b238ae
Revises: m7n8o9p0q1r2
Create Date: 2026-05-24 22:40:45.685326

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '938835b238ae'
down_revision: Union[str, Sequence[str], None] = 'm7n8o9p0q1r2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('next_up_hidden_shows', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=True))
    op.drop_index(op.f('idx_watch_events_user_completed_watched_at'), table_name='watch_events')
    op.create_index('idx_watch_events_user_completed_watched_at', 'watch_events', ['user_id', 'completed', 'watched_at'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_watch_events_user_completed_watched_at', table_name='watch_events')
    op.create_index(op.f('idx_watch_events_user_completed_watched_at'), 'watch_events', ['user_id', 'completed', sa.literal_column('watched_at DESC')], unique=False)
    op.drop_column('user_settings', 'next_up_hidden_shows')
