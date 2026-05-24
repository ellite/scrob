"""restore_media_requests

Revision ID: 11f34ec77054
Revises: 938835b238ae
Create Date: 2026-05-24 22:56:16.229465

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '11f34ec77054'
down_revision: Union[str, Sequence[str], None] = '938835b238ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS media_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tmdb_id INTEGER NOT NULL,
            media_type VARCHAR(10) NOT NULL,
            title VARCHAR(500) NOT NULL DEFAULT '',
            poster_path VARCHAR(500),
            status requeststatus NOT NULL DEFAULT 'pending',
            reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_media_requests_user_tmdb ON media_requests (user_id, tmdb_id, media_type)")


def downgrade() -> None:
    op.drop_index('ix_media_requests_user_tmdb', table_name='media_requests')
    op.drop_table('media_requests')
