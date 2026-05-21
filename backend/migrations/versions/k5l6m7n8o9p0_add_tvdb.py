"""Add TVDB support: tvdb_id to shows, make tmdb_id nullable, tvdb_api_key to settings

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'k5l6m7n8o9p0'
down_revision = 'j4k5l6m7n8o9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the NOT NULL constraint on shows.tmdb_id and make unique index deferrable-safe
    op.alter_column('shows', 'tmdb_id', nullable=True)

    # Add tvdb_id column to shows
    op.add_column('shows', sa.Column('tvdb_id', sa.Integer(), nullable=True))
    op.create_unique_constraint('uq_shows_tvdb_id', 'shows', ['tvdb_id'])

    # Add tvdb_api_key to global_settings
    op.add_column('global_settings', sa.Column('tvdb_api_key', sa.String(255), nullable=True))

    # Add tvdb_api_key to user_settings
    op.add_column('user_settings', sa.Column('tvdb_api_key', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('user_settings', 'tvdb_api_key')
    op.drop_column('global_settings', 'tvdb_api_key')
    op.drop_constraint('uq_shows_tvdb_id', 'shows', type_='unique')
    op.drop_column('shows', 'tvdb_id')
    op.alter_column('shows', 'tmdb_id', nullable=False)
