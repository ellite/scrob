"""Add connection_id FK to collection_files to track which server each file came from

Revision ID: 0005_cf_connection_id
Revises: 0004_media_server_connections
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0005_cf_connection_id'
down_revision: Union[str, Sequence[str], None] = '0004_media_server_connections'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'collection_files',
        sa.Column('connection_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_cf_connection',
        'collection_files',
        'media_server_connections',
        ['connection_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index('ix_collection_files_connection_id', 'collection_files', ['connection_id'])

    # Best-effort backfill: match existing files to connections by source type.
    # When a user has exactly one connection of that type the mapping is unambiguous.
    bind = op.get_bind()
    for src_type in ('plex', 'jellyfin', 'emby'):
        bind.execute(sa.text(f"""
            UPDATE collection_files cf
            SET connection_id = msc.id
            FROM collections c
            JOIN (
                SELECT DISTINCT ON (user_id) id, user_id
                FROM media_server_connections
                WHERE type = '{src_type}'
                ORDER BY user_id, id ASC
            ) msc ON msc.user_id = c.user_id
            WHERE cf.collection_id = c.id
              AND cf.source = '{src_type}'
              AND cf.connection_id IS NULL
              AND (
                SELECT COUNT(*) FROM media_server_connections
                WHERE user_id = c.user_id AND type = '{src_type}'
              ) = 1
        """))


def downgrade() -> None:
    op.drop_index('ix_collection_files_connection_id', 'collection_files')
    op.drop_constraint('fk_cf_connection', 'collection_files', type_='foreignkey')
    op.drop_column('collection_files', 'connection_id')
