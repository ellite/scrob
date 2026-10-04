"""simkl AUTH V2 refresh token and expiry

Revision ID: simklv2auth455
Revises: cmttvdb446
Create Date: 2026-10-04

"""
from alembic import op
import sqlalchemy as sa


revision = "simklv2auth455"
down_revision = "cmttvdb446"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both stay NULL for an AUTH V1 connection (5-year token, nothing to refresh).
    op.add_column("user_settings", sa.Column("simkl_refresh_token", sa.String(length=2000), nullable=True))
    op.add_column("user_settings", sa.Column("simkl_token_expires_at", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "simkl_token_expires_at")
    op.drop_column("user_settings", "simkl_refresh_token")
