"""add imdb_list_id to lists

Revision ID: imdblst444
Revises: tvdblst443
Create Date: 2026-09-28

"""
from alembic import op
import sqlalchemy as sa


revision = "imdblst444"
down_revision = "tvdblst443"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lists", sa.Column("imdb_list_id", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("lists", "imdb_list_id")
