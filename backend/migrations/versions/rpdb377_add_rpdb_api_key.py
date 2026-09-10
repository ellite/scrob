"""add personal RPDB API key

Revision ID: rpdb377
Revises: epgorders
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa

revision = "rpdb377"
down_revision = "epgorders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("rpdb_api_key", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "rpdb_api_key")
