"""give comments a real tvdb_id for TVDB-only shows

Revision ID: cmttvdb446
Revises: mdblglob445
Create Date: 2026-09-29

"""
from alembic import op
import sqlalchemy as sa


revision = "cmttvdb446"
down_revision = "mdblglob445"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("comments", sa.Column("tvdb_id", sa.Integer(), nullable=True))
    op.alter_column("comments", "tmdb_id", existing_type=sa.Integer(), nullable=True)
    op.create_index(
        "idx_comments_tvdb", "comments",
        ["media_type", "tvdb_id", "season_number", "episode_number"],
    )
    # TVDB-only pages used to store the show's TVDB id in tmdb_id. Move those
    # rows over - only when the number is a TVDB-only show's TVDB id and no
    # show owns it as a TMDB id, so a real TMDB-keyed comment is never touched.
    op.execute(
        """
        UPDATE comments c SET tvdb_id = c.tmdb_id, tmdb_id = NULL
        WHERE c.media_type IN ('series', 'season', 'episode')
          AND EXISTS (SELECT 1 FROM shows s WHERE s.tvdb_id = c.tmdb_id AND s.tmdb_id IS NULL)
          AND NOT EXISTS (SELECT 1 FROM shows s2 WHERE s2.tmdb_id = c.tmdb_id)
        """
    )


def downgrade() -> None:
    op.execute("UPDATE comments SET tmdb_id = tvdb_id WHERE tmdb_id IS NULL")
    op.drop_index("idx_comments_tvdb", table_name="comments")
    op.alter_column("comments", "tmdb_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("comments", "tvdb_id")
