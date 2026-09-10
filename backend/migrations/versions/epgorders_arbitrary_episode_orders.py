"""Arbitrary per-show episode orders (GitHub #174)

Generalises the binary tmdb/tvdb episode-order choice into an arbitrary set of
provider orderings (TVDB season types, TMDB episode groups).

- New `show_episode_positions` table: for a `(series_tmdb_id, order_key)` it
  holds the full ordered episode list, each row pointing at the one canonical
  episode it maps to. Aired order is the identity mapping and is never stored.
- `user_show_episode_orders.episode_order` and `ratings.episode_order` widen
  from varchar(20) to varchar(40) to fit keys like "tmdb:group:12345", and the
  legacy value "tvdb" is rewritten to "tvdb:official".
- `user_show_episode_orders` gains `order_label` (cached display name).
- The existing `tvdb:official` positions are backfilled from the untouched
  `episode_order_mappings` bridge.

Revision ID: epgorders
Revises: runtimebackfill
Create Date: 2026-09-10
"""

import sqlalchemy as sa
from alembic import op

revision = "epgorders"
down_revision = "runtimebackfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "show_episode_positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_tmdb_id", sa.Integer(), nullable=False),
        sa.Column("order_key", sa.String(length=40), nullable=False),
        sa.Column("display_season", sa.Integer(), nullable=False),
        sa.Column("display_episode", sa.Integer(), nullable=False),
        sa.Column("tmdb_episode_id", sa.Integer(), nullable=False),
        sa.Column("tmdb_season_number", sa.Integer(), nullable=False),
        sa.Column("tmdb_episode_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "series_tmdb_id", "order_key", "display_season", "display_episode",
            name="uq_show_episode_position_display",
        ),
        sa.UniqueConstraint(
            "series_tmdb_id", "order_key", "tmdb_episode_id",
            name="uq_show_episode_position_canonical",
        ),
    )
    op.create_index(
        op.f("ix_show_episode_positions_series_tmdb_id"),
        "show_episode_positions", ["series_tmdb_id"], unique=False,
    )
    op.create_index(
        "idx_show_episode_position_order",
        "show_episode_positions", ["series_tmdb_id", "order_key"], unique=False,
    )

    op.add_column(
        "user_show_episode_orders",
        sa.Column("order_label", sa.String(length=80), nullable=True),
    )
    op.alter_column(
        "user_show_episode_orders", "episode_order",
        existing_type=sa.String(length=20), type_=sa.String(length=40),
        existing_nullable=False,
    )
    op.alter_column(
        "ratings", "episode_order",
        existing_type=sa.String(length=20), type_=sa.String(length=40),
        existing_nullable=True,
    )

    op.execute(
        "UPDATE user_show_episode_orders SET episode_order = 'tvdb:official' "
        "WHERE episode_order = 'tvdb'"
    )
    op.execute(
        "UPDATE ratings SET episode_order = 'tvdb:official' "
        "WHERE episode_order = 'tvdb'"
    )

    op.execute(
        """
        INSERT INTO show_episode_positions
            (series_tmdb_id, order_key, display_season, display_episode,
             tmdb_episode_id, tmdb_season_number, tmdb_episode_number, created_at)
        SELECT series_tmdb_id, 'tvdb:official',
               tvdb_season_number, tvdb_episode_number,
               tmdb_episode_id, tmdb_season_number, tmdb_episode_number, now()
        FROM episode_order_mappings
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("UPDATE ratings SET episode_order = 'tvdb' WHERE episode_order = 'tvdb:official'")
    op.execute("DELETE FROM ratings WHERE episode_order IS NOT NULL AND episode_order <> 'tvdb'")
    op.execute("UPDATE user_show_episode_orders SET episode_order = 'tvdb' WHERE episode_order = 'tvdb:official'")
    op.execute("DELETE FROM user_show_episode_orders WHERE episode_order NOT IN ('tvdb', 'tmdb', 'tmdb:aired')")

    op.alter_column(
        "ratings", "episode_order",
        existing_type=sa.String(length=40), type_=sa.String(length=20),
        existing_nullable=True,
    )
    op.alter_column(
        "user_show_episode_orders", "episode_order",
        existing_type=sa.String(length=40), type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.drop_column("user_show_episode_orders", "order_label")

    op.drop_index("idx_show_episode_position_order", table_name="show_episode_positions")
    op.drop_index(op.f("ix_show_episode_positions_series_tmdb_id"), table_name="show_episode_positions")
    op.drop_table("show_episode_positions")
