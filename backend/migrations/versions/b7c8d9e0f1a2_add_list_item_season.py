"""add season_number to list_items

Revision ID: b7c8d9e0f1a2
Revises: e4a1c2b3d5f6
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "e4a1c2b3d5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("list_items", sa.Column("season_number", sa.Integer(), nullable=True))
    op.drop_constraint("uq_list_item", "list_items", type_="unique")
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY uq_list_item_season "
            "ON list_items (list_id, media_id, COALESCE(season_number, -1))"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_list_item_season")
    op.execute("DELETE FROM list_items WHERE season_number IS NOT NULL")
    op.create_unique_constraint("uq_list_item", "list_items", ["list_id", "media_id"])
    op.drop_column("list_items", "season_number")
