"""add wetrakr integration fields

Revision ID: wetrakr426
Revises: pushstate422
Create Date: 2026-09-26

WeTrakr sync connection — device-auth tokens + sync/push flags on
user_settings. No client_id/secret columns: Scrob ships a single app-owned
client_id baked into core/wetrakr.py, not a per-user one.
"""

from alembic import op
import sqlalchemy as sa

revision = "wetrakr426"
down_revision = "pushstate422"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("wetrakr_access_token", sa.String(2000), nullable=True))
    op.add_column("user_settings", sa.Column("wetrakr_refresh_token", sa.String(2000), nullable=True))
    op.add_column("user_settings", sa.Column("wetrakr_token_expires_at", sa.BigInteger(), nullable=True))
    op.add_column("user_settings", sa.Column("wetrakr_device_code", sa.String(255), nullable=True))
    op.add_column("user_settings", sa.Column("wetrakr_sync_watched", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("user_settings", sa.Column("wetrakr_sync_ratings", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("user_settings", sa.Column("wetrakr_push_watched", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_settings", sa.Column("wetrakr_push_ratings", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_settings", sa.Column("wetrakr_auto_sync_interval", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("wetrakr_auto_push_interval", sa.Float(), nullable=True))

    # Postgres enum values can't be dropped, so this is one-way like the
    # 'simkl' value added in g1b2c3d4e5f6_add_simkl.py.
    op.execute("ALTER TYPE collectionsource ADD VALUE IF NOT EXISTS 'wetrakr'")


def downgrade() -> None:
    op.drop_column("user_settings", "wetrakr_auto_push_interval")
    op.drop_column("user_settings", "wetrakr_auto_sync_interval")
    op.drop_column("user_settings", "wetrakr_push_ratings")
    op.drop_column("user_settings", "wetrakr_push_watched")
    op.drop_column("user_settings", "wetrakr_sync_ratings")
    op.drop_column("user_settings", "wetrakr_sync_watched")
    op.drop_column("user_settings", "wetrakr_device_code")
    op.drop_column("user_settings", "wetrakr_token_expires_at")
    op.drop_column("user_settings", "wetrakr_refresh_token")
    op.drop_column("user_settings", "wetrakr_access_token")
