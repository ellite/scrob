"""add Bingebase integration

Revision ID: a1b2c3d4e5f7
Revises: f9a2b3c4d5e6
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f7"
down_revision = "f9a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE collectionsource ADD VALUE IF NOT EXISTS 'bingebase'")
    op.add_column("user_settings", sa.Column("bingebase_webhook_url", sa.String(500), nullable=True))
    op.add_column("user_settings", sa.Column("bingebase_api_key", sa.String(255), nullable=True))
    op.add_column("user_settings", sa.Column("bingebase_scrobble", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("user_settings", sa.Column("bingebase_push_watched", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("user_settings", sa.Column("bingebase_push_ratings", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("user_settings", "bingebase_push_ratings")
    op.drop_column("user_settings", "bingebase_push_watched")
    op.drop_column("user_settings", "bingebase_scrobble")
    op.drop_column("user_settings", "bingebase_api_key")
    op.drop_column("user_settings", "bingebase_webhook_url")
