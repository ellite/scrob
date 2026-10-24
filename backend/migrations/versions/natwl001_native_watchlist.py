"""native watchlist: one per user, every service syncs with it

Adds lists.is_watchlist and a per-service reconcile baseline for Trakt,
MDBList and Simkl (Plex already has one per connection). Then folds every
managed per-service watchlist ("Trakt - Watchlist", "Plex - Watchlist",
"MDBList - Watchlist", "Simkl - Watchlist", the split Trakt pair and the
Scrob-import "Watchlist") into a new native watchlist per user, deduplicated.

The old lists are kept as plain lists, renamed with an "(old)" suffix and
unlinked from their service, so nothing is lost and nothing syncs into them
any more. The new baselines start NULL: the first reconcile per service is
purely additive, so it can't delete anything that was merged.

Revision ID: natwl001
Revises: wesrc477
Create Date: 2026-10-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "natwl001"
down_revision = "wesrc477"
branch_labels = None
depends_on = None


_LEGACY = (
    "(l.trakt_slug IN ('__watchlist__', '__watchlist_movies__', '__watchlist_shows__', "
    "'__plex_watchlist__', '__simkl_watchlist__') OR l.mdblist_slug = '__watchlist__')"
)


def upgrade() -> None:
    op.add_column("lists", sa.Column("is_watchlist", sa.Boolean(), nullable=False, server_default="false"))
    op.create_index(
        "uq_lists_user_watchlist", "lists", ["user_id"], unique=True,
        postgresql_where=sa.text("is_watchlist"),
    )
    op.add_column("user_settings", sa.Column("trakt_watchlist_synced_keys", JSONB(), nullable=True))
    op.add_column("user_settings", sa.Column("mdblist_watchlist_synced_keys", JSONB(), nullable=True))
    op.add_column("user_settings", sa.Column("simkl_watchlist_synced_keys", JSONB(), nullable=True))

    op.execute(f"""
        INSERT INTO lists (user_id, name, privacy_level, is_watchlist, created_at, updated_at)
        SELECT l.user_id, 'Watchlist', 'private', true, MIN(l.created_at), now()
        FROM lists l
        WHERE {_LEGACY}
        GROUP BY l.user_id
    """)

    # Earliest copy of each title wins, so its added date and notes carry over.
    op.execute(f"""
        INSERT INTO list_items (list_id, media_id, season_number, added_at, sort_order, notes)
        SELECT w.id, li.media_id, li.season_number, li.added_at,
               ROW_NUMBER() OVER (PARTITION BY w.id ORDER BY li.added_at, li.id) - 1,
               li.notes
        FROM (
            SELECT DISTINCT ON (l.user_id, li.media_id, COALESCE(li.season_number, -1))
                   l.user_id, li.id, li.media_id, li.season_number, li.added_at, li.notes
            FROM list_items li
            JOIN lists l ON l.id = li.list_id
            WHERE {_LEGACY}
            ORDER BY l.user_id, li.media_id, COALESCE(li.season_number, -1), li.added_at, li.id
        ) li
        JOIN lists w ON w.user_id = li.user_id AND w.is_watchlist
        ON CONFLICT DO NOTHING
    """)

    op.execute(f"""
        UPDATE lists l
        SET name = LEFT(l.name, 249) || ' (old)', trakt_slug = NULL, mdblist_slug = NULL
        WHERE {_LEGACY}
    """)


def downgrade() -> None:
    # The native list becomes the old code's Trakt-managed watchlist, so a
    # later upgrade picks it up again. The "(old)" lists stay unlinked.
    op.execute("UPDATE lists SET trakt_slug = '__watchlist__' WHERE is_watchlist")
    op.drop_column("user_settings", "simkl_watchlist_synced_keys")
    op.drop_column("user_settings", "mdblist_watchlist_synced_keys")
    op.drop_column("user_settings", "trakt_watchlist_synced_keys")
    op.drop_index("uq_lists_user_watchlist", table_name="lists")
    op.drop_column("lists", "is_watchlist")
