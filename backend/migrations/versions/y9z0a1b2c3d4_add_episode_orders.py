"""add per-show episode orders

Revision ID: y9z0a1b2c3d4
Revises: x8y9z0a1b2c3
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa


revision = "y9z0a1b2c3d4"
down_revision = "x8y9z0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_show_episode_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("series_tmdb_id", sa.Integer(), nullable=False),
        sa.Column("episode_order", sa.String(length=20), nullable=False),
        sa.Column("tvdb_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "series_tmdb_id", name="uq_user_show_episode_order"),
    )
    op.create_index(
        op.f("ix_user_show_episode_orders_user_id"),
        "user_show_episode_orders",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_show_episode_orders_series_tmdb_id"),
        "user_show_episode_orders",
        ["series_tmdb_id"],
        unique=False,
    )

    op.create_table(
        "episode_order_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_tmdb_id", sa.Integer(), nullable=False),
        sa.Column("tmdb_season_number", sa.Integer(), nullable=False),
        sa.Column("tmdb_episode_number", sa.Integer(), nullable=False),
        sa.Column("tmdb_episode_id", sa.Integer(), nullable=False),
        sa.Column("tvdb_id", sa.Integer(), nullable=False),
        sa.Column("tvdb_season_number", sa.Integer(), nullable=False),
        sa.Column("tvdb_episode_number", sa.Integer(), nullable=False),
        sa.Column("match_method", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "series_tmdb_id",
            "tmdb_season_number",
            "tmdb_episode_number",
            name="uq_episode_order_mapping_tmdb",
        ),
        sa.UniqueConstraint(
            "series_tmdb_id",
            "tvdb_id",
            name="uq_episode_order_mapping_tvdb_id",
        ),
    )
    op.create_index(
        op.f("ix_episode_order_mappings_series_tmdb_id"),
        "episode_order_mappings",
        ["series_tmdb_id"],
        unique=False,
    )
    op.create_index(
        "idx_episode_order_mapping_tvdb_position",
        "episode_order_mappings",
        ["series_tmdb_id", "tvdb_season_number", "tvdb_episode_number"],
        unique=False,
    )

    op.add_column("ratings", sa.Column("episode_order", sa.String(length=20), nullable=True))
    op.drop_index("uq_rating_user_media_season", table_name="ratings")
    op.execute(
        "CREATE UNIQUE INDEX uq_rating_user_media_season_order "
        "ON ratings (user_id, media_id, COALESCE(season_number, -1), "
        "COALESCE(episode_order, 'tmdb'))"
    )


def downgrade() -> None:
    op.drop_index("uq_rating_user_media_season_order", table_name="ratings")
    op.execute("DELETE FROM ratings WHERE episode_order IS NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX uq_rating_user_media_season "
        "ON ratings (user_id, media_id, COALESCE(season_number, -1))"
    )
    op.drop_column("ratings", "episode_order")
    op.drop_index("idx_episode_order_mapping_tvdb_position", table_name="episode_order_mappings")
    op.drop_index(op.f("ix_episode_order_mappings_series_tmdb_id"), table_name="episode_order_mappings")
    op.drop_table("episode_order_mappings")
    op.drop_index(op.f("ix_user_show_episode_orders_series_tmdb_id"), table_name="user_show_episode_orders")
    op.drop_index(op.f("ix_user_show_episode_orders_user_id"), table_name="user_show_episode_orders")
    op.drop_table("user_show_episode_orders")
