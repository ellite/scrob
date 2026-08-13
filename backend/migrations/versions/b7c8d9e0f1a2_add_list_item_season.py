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
    # Idempotent/resumable: CREATE INDEX CONCURRENTLY below can't run inside a
    # transaction, so entering autocommit_block() commits add_column/drop_constraint
    # first. If the concurrent index build is then interrupted (container restart,
    # OOM, etc.) before alembic_version advances, a retry replays this whole
    # function from the top - IF [NOT] EXISTS on every step makes that safe
    # instead of crashing with "column already exists" (see #183).
    op.execute("ALTER TABLE list_items ADD COLUMN IF NOT EXISTS season_number INTEGER")
    op.execute("ALTER TABLE list_items DROP CONSTRAINT IF EXISTS uq_list_item")
    with op.get_context().autocommit_block():
        # A prior interrupted build leaves an INVALID index behind under the same
        # name - IF NOT EXISTS alone would then skip recreating it, silently
        # leaving the uniqueness constraint unenforced.
        conn = op.get_bind()
        invalid = conn.execute(sa.text(
            "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = 'uq_list_item_season' AND NOT i.indisvalid"
        )).scalar()
        if invalid:
            op.execute("DROP INDEX CONCURRENTLY uq_list_item_season")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_list_item_season "
            "ON list_items (list_id, media_id, COALESCE(season_number, -1))"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_list_item_season")
    op.execute("DELETE FROM list_items WHERE season_number IS NOT NULL")
    op.execute("ALTER TABLE list_items ADD CONSTRAINT uq_list_item UNIQUE (list_id, media_id)")
    op.execute("ALTER TABLE list_items DROP COLUMN IF EXISTS season_number")
