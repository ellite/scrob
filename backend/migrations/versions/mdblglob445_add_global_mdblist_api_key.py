"""add global MDBList API key

Revision ID: mdblglob445
Revises: imdblst444
Create Date: 2026-09-28

"""
from alembic import op
import sqlalchemy as sa


revision = "mdblglob445"
down_revision = "imdblst444"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "global_settings",
        sa.Column("mdblist_api_key", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("global_settings", "mdblist_api_key")
