"""add tmdb_list_id to lists

Revision ID: tmdblst442
Revises: trktcm434
Create Date: 2026-09-28

"""
from alembic import op
import sqlalchemy as sa


revision = "tmdblst442"
down_revision = "trktcm434"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lists", sa.Column("tmdb_list_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("lists", "tmdb_list_id")
