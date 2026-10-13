"""selectable rating source (MDBList) + external_ratings cache

Revision ID: ratingsource
Revises: wetrakrlists
Create Date: 2026-10-13

user_settings.rating_source / rating_show_tmdb pick which rating the
poster badges and detail pages show. external_ratings caches MDBList's
per-title ratings (all sources in one row) so badges render without an
MDBList call on every page view.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "ratingsource"
down_revision = "wetrakrlists"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("rating_source", sa.String(length=20), nullable=False, server_default="tmdb"))
    op.add_column("user_settings", sa.Column("rating_show_tmdb", sa.Boolean(), nullable=False, server_default="false"))
    op.create_table(
        "external_ratings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=10), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("ratings", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("fetched_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("media_type", "tmdb_id", name="uq_external_ratings_title"),
    )
    op.create_index("ix_external_ratings_fetched_at", "external_ratings", ["fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_external_ratings_fetched_at", table_name="external_ratings")
    op.drop_table("external_ratings")
    op.drop_column("user_settings", "rating_show_tmdb")
    op.drop_column("user_settings", "rating_source")
