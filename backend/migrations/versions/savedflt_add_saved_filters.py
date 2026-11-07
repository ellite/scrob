"""add saved_filters

Revision ID: savedflt
Revises: wesrc477
Create Date: 2026-10-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "savedflt"
down_revision = "wesrc477"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_filters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "scope", "name", name="uq_saved_filter_name"),
    )
    op.create_index("ix_saved_filters_user_id", "saved_filters", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_saved_filters_user_id", table_name="saved_filters")
    op.drop_table("saved_filters")
