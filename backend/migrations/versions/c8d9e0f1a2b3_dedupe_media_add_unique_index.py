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

    # media_translations: unique on (media_id, language). A dedup group can
    # have more than two rows (three or more Media rows sharing a tmdb_id),
    # so it's not enough to check each loser against the winner alone - two
    # losers in the same group can each hold a translation for the same
    # language that collides with neither the winner nor each other *until*
    # both get repointed onto the winner by the same UPDATE. Drop a loser's
    # translation when the winner already has one for that language, or when
    # another loser in the same group has a lower id for that same language
    # (keeping exactly one candidate per winner+language to repoint).
    op.execute(
        """
        DELETE FROM media_translations mt
        USING media_dedup d
        WHERE mt.media_id = d.loser_id
          AND (
              EXISTS (
                  SELECT 1 FROM media_translations mt2
                  WHERE mt2.media_id = d.winner_id AND mt2.language = mt.language
              )
              OR EXISTS (
                  SELECT 1 FROM media_translations mt3
                  JOIN media_dedup d3 ON mt3.media_id = d3.loser_id
                  WHERE d3.winner_id = d.winner_id
                    AND mt3.language = mt.language
                    AND mt3.id < mt.id
              )
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
    # (same season, if a season entry) or another loser in the group already
    # claims the same list+season and has a lower id (see media_translations
    # above for why a group can have more than one loser), repoint the rest.
    op.execute(
        """
        DELETE FROM list_items li
        USING media_dedup d
        WHERE li.media_id = d.loser_id
          AND (
              EXISTS (
                  SELECT 1 FROM list_items li2
                  WHERE li2.media_id = d.winner_id
                    AND li2.list_id = li.list_id
                    AND COALESCE(li2.season_number, -1) = COALESCE(li.season_number, -1)
              )
              OR EXISTS (
                  SELECT 1 FROM list_items li3
                  JOIN media_dedup d3 ON li3.media_id = d3.loser_id
                  WHERE d3.winner_id = d.winner_id
                    AND li3.list_id = li.list_id
                    AND COALESCE(li3.season_number, -1) = COALESCE(li.season_number, -1)
                    AND li3.id < li.id
              )
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
    # already rated the winner on that same key, or another loser in the group
    # already claims the same key and has a lower id, repoint the rest.
    op.execute(
        """
        DELETE FROM ratings r
        USING media_dedup d
        WHERE r.media_id = d.loser_id
          AND (
              EXISTS (
                  SELECT 1 FROM ratings r2
                  WHERE r2.media_id = d.winner_id
                    AND r2.user_id = r.user_id
                    AND COALESCE(r2.season_number, -1) = COALESCE(r.season_number, -1)
                    AND COALESCE(r2.episode_order, 'tmdb') = COALESCE(r.episode_order, 'tmdb')
              )
              OR EXISTS (
                  SELECT 1 FROM ratings r3
                  JOIN media_dedup d3 ON r3.media_id = d3.loser_id
                  WHERE d3.winner_id = d.winner_id
                    AND r3.user_id = r.user_id
                    AND COALESCE(r3.season_number, -1) = COALESCE(r.season_number, -1)
                    AND COALESCE(r3.episode_order, 'tmdb') = COALESCE(r.episode_order, 'tmdb')
                    AND r3.id < r.id
              )
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
    # progress row where the winner already has one for that rewatch cycle,
    # or another loser in the group already claims the same rewatch cycle and
    # has a lower id.
    op.execute(
        """
        DELETE FROM rewatch_progress rp
        USING media_dedup d
        WHERE rp.media_id = d.loser_id
          AND (
              EXISTS (
                  SELECT 1 FROM rewatch_progress rp2
                  WHERE rp2.media_id = d.winner_id AND rp2.rewatch_id = rp.rewatch_id
              )
              OR EXISTS (
                  SELECT 1 FROM rewatch_progress rp3
                  JOIN media_dedup d3 ON rp3.media_id = d3.loser_id
                  WHERE d3.winner_id = d.winner_id
                    AND rp3.rewatch_id = rp.rewatch_id
                    AND rp3.id < rp.id
              )
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
    # in-progress marker where the user already has one against the winner,
    # or another loser in the group already claims the same user and has a
    # lower id.
    op.execute(
        """
        DELETE FROM playback_progress pp
        USING media_dedup d
        WHERE pp.media_id = d.loser_id
          AND (
              EXISTS (
                  SELECT 1 FROM playback_progress pp2
                  WHERE pp2.media_id = d.winner_id AND pp2.user_id = pp.user_id
              )
              OR EXISTS (
                  SELECT 1 FROM playback_progress pp3
                  JOIN media_dedup d3 ON pp3.media_id = d3.loser_id
                  WHERE d3.winner_id = d.winner_id
                    AND pp3.user_id = pp.user_id
                    AND pp3.id < pp.id
              )
          )
        """
    )
    op.execute(
        """
        UPDATE playback_progress pp SET media_id = d.winner_id
        FROM media_dedup d WHERE pp.media_id = d.loser_id
        """
    )

    # collections: unique on (user_id, media_id). Unlike the tables above, a
    # collision here can't just be dropped in favor of the winner's row -
    # collections carries child collection_files that need merging first. And
    # since a dedup group can span more than two Media rows, a user may have
    # separately collected *two or more* losers with no collections row on
    # the winner at all - a plain "repoint onto the winner" would then try to
    # create two rows for the same (user, winner) pair in one UPDATE. So:
    # pick one canonical target collection per (winner, user) up front - the
    # winner's own row if it has one, else the lowest-id row among the
    # group's members - merge every other member's files into it, then
    # collapse every non-target row in the group onto it.
    op.execute(
        """
        CREATE TEMP TABLE media_group_members AS
        SELECT winner_id, winner_id AS member_id FROM (SELECT DISTINCT winner_id FROM media_dedup) w
        UNION ALL
        SELECT winner_id, loser_id AS member_id FROM media_dedup
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE collection_targets AS
        SELECT DISTINCT ON (g.winner_id, c.user_id)
            g.winner_id, c.user_id, c.id AS target_id
        FROM media_group_members g
        JOIN collections c ON c.media_id = g.member_id
        ORDER BY g.winner_id, c.user_id, (g.member_id = g.winner_id) DESC, c.id ASC
        """
    )
    # Move every other group member's collection_files onto the target,
    # dropping any that collide with a file the target already has from the
    # same source.
    op.execute(
        """
        UPDATE collection_files cf
        SET collection_id = ct.target_id
        FROM media_group_members g
        JOIN collections c ON c.media_id = g.member_id
        JOIN collection_targets ct ON ct.winner_id = g.winner_id AND ct.user_id = c.user_id
        WHERE cf.collection_id = c.id
          AND c.id != ct.target_id
          AND NOT EXISTS (
              SELECT 1 FROM collection_files cf2
              WHERE cf2.collection_id = ct.target_id
                AND cf2.source = cf.source
                AND cf2.source_id IS NOT DISTINCT FROM cf.source_id
          )
        """
    )
    # Any collection_files still attached to a non-target collection at this
    # point are the ones that collided above - drop them, the target already
    # has an equivalent file from that same source.
    op.execute(
        """
        DELETE FROM collection_files cf
        USING media_group_members g, collections c, collection_targets ct
        WHERE c.media_id = g.member_id
          AND ct.winner_id = g.winner_id AND ct.user_id = c.user_id
          AND cf.collection_id = c.id
          AND c.id != ct.target_id
        """
    )
    # Repoint the target row itself onto the winner Media row - only changes
    # anything when the target came from a loser (i.e. the winner had no
    # collections row of its own for that user).
    op.execute(
        """
        UPDATE collections c
        SET media_id = ct.winner_id
        FROM collection_targets ct
        WHERE c.id = ct.target_id AND c.media_id != ct.winner_id
        """
    )
    # Every other collections row in the group is now empty - drop it.
    op.execute(
        """
        DELETE FROM collections c
        USING media_group_members g, collection_targets ct
        WHERE c.media_id = g.member_id
          AND ct.winner_id = g.winner_id AND ct.user_id = c.user_id
          AND c.id != ct.target_id
        """
    )
    op.execute("DROP TABLE collection_targets")
    op.execute("DROP TABLE media_group_members")

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
