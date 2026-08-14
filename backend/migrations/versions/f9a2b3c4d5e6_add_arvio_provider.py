"""add ARVIO provider

Revision ID: f9a2b3c4d5e6
Revises: e4a1c2b3d5f6
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa


revision = "f9a2b3c4d5e6"
down_revision = "e4a1c2b3d5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE collectionsource ADD VALUE IF NOT EXISTS 'arvio'")
    op.drop_constraint("ck_msc_type", "media_server_connections", type_="check")
    op.create_check_constraint(
        "ck_msc_type",
        "media_server_connections",
        "type IN ('plex', 'jellyfin', 'emby', 'nuvio', 'stremio', 'arvio')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_msc_type", "media_server_connections", type_="check")
    op.create_check_constraint(
        "ck_msc_type",
        "media_server_connections",
        "type IN ('plex', 'jellyfin', 'emby', 'nuvio', 'stremio')",
    )
