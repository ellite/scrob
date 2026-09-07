"""Add source-agnostic tracker items.

Revision ID: a4b91c2d3e4f
Revises: we355created
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a4b91c2d3e4f"
down_revision = "we355created"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("original_title", sa.String(length=500)),
        sa.Column("cover_url", sa.String(length=1000)),
        sa.Column("description", sa.Text()),
        sa.Column("release_date", sa.String(length=20)),
        sa.Column("total_units", sa.Integer()),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("source", "external_id", "media_type", name="uq_catalog_item_identity"),
    )
    op.create_index("idx_catalog_items_type_title", "catalog_items", ["media_type", "title"])
    op.create_table(
        "tracker_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="planning", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("score", sa.Float()),
        sa.Column("notes", sa.Text()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('current', 'completed', 'planning', 'paused', 'dropped', 'repeating')", name="ck_tracker_entries_status"),
        sa.CheckConstraint("progress >= 0", name="ck_tracker_entries_progress"),
        sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_tracker_entries_score"),
        sa.UniqueConstraint("user_id", "item_id", name="uq_tracker_entry_user_item"),
    )
    op.create_index("idx_tracker_entries_user_status", "tracker_entries", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_tracker_entries_user_status", table_name="tracker_entries")
    op.drop_table("tracker_entries")
    op.drop_index("idx_catalog_items_type_title", table_name="catalog_items")
    op.drop_table("catalog_items")
