"""add tvdb_list_id to lists

Revision ID: tvdblst443
Revises: tmdblst442
Create Date: 2026-09-28

"""
from alembic import op
import sqlalchemy as sa


revision = "tvdblst443"
down_revision = "tmdblst442"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lists", sa.Column("tvdb_list_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("lists", "tvdb_list_id")
