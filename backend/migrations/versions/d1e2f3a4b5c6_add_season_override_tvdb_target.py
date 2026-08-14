"""add tvdb target to show_season_overrides

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
Create Date: 2026-08-15
"""

from alembic import op


revision = "d1e2f3a4b5c6"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A remap target is now either a TMDB show or a TVDB show (#178) - exactly
    # one of target_show_tmdb_id/target_show_tvdb_id is set, enforced at the
    # API layer rather than a DB constraint (matches Show.tmdb_id/tvdb_id,
    # which use the same either-or convention with no DB-level check).
    op.execute("ALTER TABLE show_season_overrides ALTER COLUMN target_show_tmdb_id DROP NOT NULL")
    op.execute("ALTER TABLE show_season_overrides ADD COLUMN IF NOT EXISTS target_show_tvdb_id INTEGER")


def downgrade() -> None:
    op.execute("DELETE FROM show_season_overrides WHERE target_show_tmdb_id IS NULL")
    op.execute("ALTER TABLE show_season_overrides DROP COLUMN IF EXISTS target_show_tvdb_id")
    op.execute("ALTER TABLE show_season_overrides ALTER COLUMN target_show_tmdb_id SET NOT NULL")
