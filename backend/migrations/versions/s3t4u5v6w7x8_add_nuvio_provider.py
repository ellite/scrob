"""add Nuvio provider

Revision ID: s3t4u5v6w7x8
Revises: m7n8o9p0q1r2, r2s3t4u5v6w7
Create Date: 2026-07-14
"""

from alembic import op


revision = "s3t4u5v6w7x8"
down_revision = ("m7n8o9p0q1r2", "r2s3t4u5v6w7")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE collectionsource ADD VALUE IF NOT EXISTS 'nuvio'")
    op.drop_constraint("ck_msc_type", "media_server_connections", type_="check")
    op.create_check_constraint(
        "ck_msc_type",
        "media_server_connections",
        "type IN ('plex', 'jellyfin', 'emby', 'nuvio')",
    )


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely in-place. Keep the value and
    # the compatible constraint; older application versions simply do not create it.
    pass
