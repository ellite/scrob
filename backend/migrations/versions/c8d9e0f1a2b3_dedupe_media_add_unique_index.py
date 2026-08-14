"""merge duplicate Media rows and add a unique index on (tmdb_id, media_type)

Concurrent/inconsistent "find or create episode" logic across many call sites
(webhooks, sync, Trakt/Simkl import, manual watch endpoints) can race and create
more than one Media row for the same (tmdb_id, media_type) - most commonly
episodes. Nothing at the DB level ever caught this, so it silently split watch
history, ratings, and collection state across the duplicate rows (see #157).

This migration merges every such duplicate group onto one canonical "winner"
row (the lowest id) before adding a unique index that makes it impossible for
new duplicates to be created going forward. Rows in dependent tables are
repointed onto the winner; where merging would collide with a unique
constraint on the winner's side (e.g. the same user already has both rows in
a list/rated/collected), the loser's now-redundant row is dropped instead of
repointed, since the winner's equivalent row already covers it.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-11
"""

from alembic import op


revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per "loser" Media id, pointing at the "winner" (lowest id) for
    # its (tmdb_id, media_type) group. Only real duplicate groups appear here.
    #
    # Sharing a tmdb_id is necessary but not sufficient proof two rows are the
    # same episode - a run against real data turned up a handful of groups
    # where season_number/episode_number disagreed (most commonly TMDB having
    # re-numbered a show's episodes between two ingestion times, but including
    # at least one outright coincidental id collision between two unrelated
    # episodes). Merging those would silently combine two different episodes'
    # watch/rating/collection history into one row - worse than the duplicate
    # rows this migration is meant to fix. So only merge when season_number
    # and episode_number also agree (show_id is allowed to differ - that's a
    # separate, harmless case of a duplicate local Show row for the same
    # actual show). Movies have no season/episode to disagree on, so they're
    # unaffected by this guard.
    op.execute(
        """
        CREATE TEMP TABLE media_dedup AS
        SELECT loser_id, winner_id FROM (
            SELECT
                m.id AS loser_id,
                first_value(m.id) OVER w AS winner_id,
                m.season_number,
                m.episode_number,
                first_value(m.season_number) OVER w AS winner_season_number,
                first_value(m.episode_number) OVER w AS winner_episode_number
            FROM media m
            WHERE m.tmdb_id IS NOT NULL
            WINDOW w AS (PARTITION BY m.tmdb_id, m.media_type ORDER BY m.id)
        ) t
        WHERE loser_id != winner_id
          AND season_number IS NOT DISTINCT FROM winner_season_number
          AND episode_number IS NOT DISTINCT FROM winner_episode_number
        """
    )

    # The season/episode-mismatched rows excluded above still share a tmdb_id
    # with another row and would still violate the unique index added at the
    # end of this migration once the safe merges are deleted. Since merging
    # them isn't safe, null out their tmdb_id instead - this only removes an
    # identifier that's already proven unreliable for that row; the row, its
    # title, and all of its watch/rating/collection history are untouched, and
    # every other lookup path (show_id + season + episode) keeps working.
    op.execute(
        """
        CREATE TEMP TABLE media_tmdb_unlink AS
        SELECT id FROM (
            SELECT m.id, first_value(m.id) OVER w AS winner_id
            FROM media m
            WHERE m.tmdb_id IS NOT NULL
            WINDOW w AS (PARTITION BY m.tmdb_id, m.media_type ORDER BY m.id)
        ) t
        WHERE id != winner_id AND id NOT IN (SELECT loser_id FROM media_dedup)
        """
    )

    # watch_events: no uniqueness on (user_id, media_id) - multiple plays of the
    # same media are legitimate (rewatches) - so this is a plain repoint.
    op.execute(
        """
        UPDATE watch_events w SET media_id = d.winner_id
        FROM media_dedup d WHERE w.media_id = d.loser_id
        """
    )

    # playback_sessions: uniqueness is on session_key only, not media_id - plain repoint.
    op.execute(
        """
        UPDATE playback_sessions p SET media_id = d.winner_id
        FROM media_dedup d WHERE p.media_id = d.loser_id
        """
    )

    # media_translations: unique on (media_id, language) - drop the loser's
    # translation where the winner already has one for that language, repoint
    # the rest.
    op.execute(
        """
        DELETE FROM media_translations mt
        USING media_dedup d
        WHERE mt.media_id = d.loser_id
          AND EXISTS (
              SELECT 1 FROM media_translations mt2
              WHERE mt2.media_id = d.winner_id AND mt2.language = mt.language
          )
        """
    )
    op.execute(
        """
        UPDATE media_translations mt SET media_id = d.winner_id
        FROM media_dedup d WHERE mt.media_id = d.loser_id
        """
    )

    # list_items: unique on (list_id, media_id, COALESCE(season_number, -1)) -
    # drop the loser's entry where the winner is already on that same list
    # (same season, if a season entry), repoint the rest.
    op.execute(
        """
        DELETE FROM list_items li
        USING media_dedup d
        WHERE li.media_id = d.loser_id
          AND EXISTS (
              SELECT 1 FROM list_items li2
              WHERE li2.media_id = d.winner_id
                AND li2.list_id = li.list_id
                AND COALESCE(li2.season_number, -1) = COALESCE(li.season_number, -1)
          )
        """
    )
    op.execute(
        """
        UPDATE list_items li SET media_id = d.winner_id
        FROM media_dedup d WHERE li.media_id = d.loser_id
        """
    )

    # ratings: unique on (user_id, media_id, COALESCE(season_number, -1),
    # COALESCE(episode_order, 'tmdb')) - drop the loser's rating where the user
    # already rated the winner on that same key, repoint the rest.
    op.execute(
        """
        DELETE FROM ratings r
        USING media_dedup d
        WHERE r.media_id = d.loser_id
          AND EXISTS (
              SELECT 1 FROM ratings r2
              WHERE r2.media_id = d.winner_id
                AND r2.user_id = r.user_id
                AND COALESCE(r2.season_number, -1) = COALESCE(r.season_number, -1)
                AND COALESCE(r2.episode_order, 'tmdb') = COALESCE(r.episode_order, 'tmdb')
          )
        """
    )
    op.execute(
        """
        UPDATE ratings r SET media_id = d.winner_id
        FROM media_dedup d WHERE r.media_id = d.loser_id
        """
    )

    # rewatch_progress: unique on (rewatch_id, media_id) - drop the loser's
    # progress row where the winner already has one for that rewatch cycle.
    op.execute(
        """
        DELETE FROM rewatch_progress rp
        USING media_dedup d
        WHERE rp.media_id = d.loser_id
          AND EXISTS (
              SELECT 1 FROM rewatch_progress rp2
              WHERE rp2.media_id = d.winner_id AND rp2.rewatch_id = rp.rewatch_id
          )
        """
    )
    op.execute(
        """
        UPDATE rewatch_progress rp SET media_id = d.winner_id
        FROM media_dedup d WHERE rp.media_id = d.loser_id
        """
    )

    # playback_progress: unique on (user_id, media_id) - drop the loser's
    # in-progress marker where the user already has one against the winner.
    op.execute(
        """
        DELETE FROM playback_progress pp
        USING media_dedup d
        WHERE pp.media_id = d.loser_id
          AND EXISTS (
              SELECT 1 FROM playback_progress pp2
              WHERE pp2.media_id = d.winner_id AND pp2.user_id = pp.user_id
          )
        """
    )
    op.execute(
        """
        UPDATE playback_progress pp SET media_id = d.winner_id
        FROM media_dedup d WHERE pp.media_id = d.loser_id
        """
    )

    # collections: unique on (user_id, media_id). Two cases:
    #  1. The user only collected the loser -> plain repoint.
    #  2. The user collected both -> merge: move the loser's CollectionFile
    #     rows onto the winner's collection (dropping any that collide with a
    #     file the winner's collection already has from the same source), then
    #     delete the now-empty loser collection row.
    op.execute(
        """
        UPDATE collections c SET media_id = d.winner_id
        FROM media_dedup d
        WHERE c.media_id = d.loser_id
          AND NOT EXISTS (
              SELECT 1 FROM collections c2
              WHERE c2.media_id = d.winner_id AND c2.user_id = c.user_id
          )
        """
    )
    op.execute(
        """
        UPDATE collection_files cf
        SET collection_id = c_winner.id
        FROM media_dedup d
        JOIN collections c_loser ON c_loser.media_id = d.loser_id
        JOIN collections c_winner
            ON c_winner.media_id = d.winner_id AND c_winner.user_id = c_loser.user_id
        WHERE cf.collection_id = c_loser.id
          AND NOT EXISTS (
              SELECT 1 FROM collection_files cf2
              WHERE cf2.collection_id = c_winner.id
                AND cf2.source = cf.source
                AND cf2.source_id IS NOT DISTINCT FROM cf.source_id
          )
        """
    )
    # Any collection_files still attached to a (still-existing) loser collection
    # at this point are the ones that collided above - drop them, the winner's
    # collection already has an equivalent file from that same source.
    op.execute(
        """
        DELETE FROM collection_files cf
        USING media_dedup d, collections c_loser
        WHERE c_loser.media_id = d.loser_id
          AND cf.collection_id = c_loser.id
        """
    )
    op.execute(
        """
        DELETE FROM collections c
        USING media_dedup d
        WHERE c.media_id = d.loser_id
          AND EXISTS (
              SELECT 1 FROM collections c2
              WHERE c2.media_id = d.winner_id AND c2.user_id = c.user_id
          )
        """
    )

    # Every dependent table has been repointed or reconciled - the loser rows
    # are now unreferenced and safe to delete.
    op.execute("DELETE FROM media m USING media_dedup d WHERE m.id = d.loser_id")

    op.execute(
        "UPDATE media SET tmdb_id = NULL WHERE id IN (SELECT id FROM media_tmdb_unlink)"
    )

    op.execute("DROP TABLE media_dedup")
    op.execute("DROP TABLE media_tmdb_unlink")

    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY uq_media_tmdb_type "
            "ON media (tmdb_id, media_type) WHERE tmdb_id IS NOT NULL"
        )


def downgrade() -> None:
    # The row merge itself isn't reversible (the original duplicate split is
    # gone) - only the constraint that prevents new duplicates is.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_media_tmdb_type")
