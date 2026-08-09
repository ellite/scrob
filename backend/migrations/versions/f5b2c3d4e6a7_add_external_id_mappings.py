"""add external id -> tmdb id mapping cache

Revision ID: f5b2c3d4e6a7
Revises: e4a1c2b3d5f6
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa


revision = "f5b2c3d4e6a7"
down_revision = "e4a1c2b3d5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_id_mappings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("media_kind", sa.String(length=20), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("miss_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source", "external_id", "media_kind", name="uq_external_id_mappings_lookup"),
    )
    op.create_index(
        "idx_external_id_mappings_misses",
        "external_id_mappings",
        ["checked_at"],
        postgresql_where=sa.text("tmdb_id IS NULL"),
    )
    op.create_index(
        "idx_external_id_mappings_reverse",
        "external_id_mappings",
        ["source", "media_kind", "tmdb_id"],
    )

    # ── Backfill ────────────────────────────────────────────────────────────
    # Seed from mappings we already hold so existing installs start warm rather
    # than re-resolving their whole library through TMDB once more.

    # 1. shows.tvdb_id -> shows.tmdb_id. Both are dedicated indexed columns and
    #    a row with both set is either a TMDB cross-reference or an explicit
    #    user match, so it is at least as trustworthy as /find.
    op.execute(
        """
        INSERT INTO external_id_mappings
            (source, external_id, media_kind, tmdb_id, miss_count, checked_at, created_at)
        SELECT 'tvdb_id', s.tvdb_id::text, 'tv', s.tmdb_id, 0, now(), now()
          FROM shows s
         WHERE s.tvdb_id IS NOT NULL AND s.tmdb_id IS NOT NULL
        ON CONFLICT ON CONSTRAINT uq_external_id_mappings_lookup DO NOTHING
        """
    )

    # 2. IMDb ids the outbound push path already persisted into tmdb_data but
    #    which the inbound path never read back.
    op.execute(
        """
        INSERT INTO external_id_mappings
            (source, external_id, media_kind, tmdb_id, miss_count, checked_at, created_at)
        SELECT DISTINCT ON (2) 'imdb_id', s.tmdb_data->'external_ids'->>'imdb_id', 'tv',
               s.tmdb_id, 0, now(), now()
          FROM shows s
         WHERE s.tmdb_id IS NOT NULL
           AND s.tmdb_data->'external_ids'->>'imdb_id' ~ '^tt[0-9]+$'
        ON CONFLICT ON CONSTRAINT uq_external_id_mappings_lookup DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO external_id_mappings
            (source, external_id, media_kind, tmdb_id, miss_count, checked_at, created_at)
        SELECT DISTINCT ON (2) 'imdb_id', m.tmdb_data->'external_ids'->>'imdb_id', 'movie',
               m.tmdb_id, 0, now(), now()
          FROM media m
         WHERE m.tmdb_id IS NOT NULL
           AND m.media_type = 'movie'
           AND m.tmdb_data->'external_ids'->>'imdb_id' ~ '^tt[0-9]+$'
           AND COALESCE(m.tmdb_data->>'source', '') <> 'tvdb'
        ON CONFLICT ON CONSTRAINT uq_external_id_mappings_lookup DO NOTHING
        """
    )

    # 3. Replaces the CollectionFile read-back that _resolve_nuvio_tmdb_ids used
    #    as an ad-hoc cache, so Nuvio/Stremio users keep their existing hit rate.
    #
    #    The two predicates below are CRITICAL, not defensive:
    #
    #    * media_type IN ('movie','series') excludes episodes. For an episode the
    #      source_id is "{profile}:{show_imdb}:sXeY" but the joined media row is
    #      the EPISODE, so the old read-back mapped a show's IMDb id to an
    #      episode's TMDB id and fed that to sync_shows_batch. Without this
    #      filter the migration would persist that bug globally instead of
    #      fixing it.
    #    * the tmdb_data source check excludes TVDB-enriched rows, whose
    #      media.tmdb_id is actually a TVDB episode id (see
    #      core.enrichment.enrich_episode_from_tvdb).
    op.execute(
        """
        INSERT INTO external_id_mappings
            (source, external_id, media_kind, tmdb_id, miss_count, checked_at, created_at)
        SELECT DISTINCT ON (2, 3)
               'imdb_id',
               split_part(cf.source_id, ':', 2),
               CASE WHEN m.media_type = 'movie' THEN 'movie' ELSE 'tv' END,
               m.tmdb_id, 0, now(), now()
          FROM collection_files cf
          JOIN collections c ON c.id = cf.collection_id
          JOIN media m       ON m.id = c.media_id
         WHERE cf.source IN ('nuvio', 'stremio')
           AND m.tmdb_id IS NOT NULL
           AND m.media_type IN ('movie', 'series')
           AND COALESCE(m.tmdb_data->>'source', '') <> 'tvdb'
           AND split_part(cf.source_id, ':', 2) ~ '^tt[0-9]+$'
        ON CONFLICT ON CONSTRAINT uq_external_id_mappings_lookup DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("idx_external_id_mappings_reverse", table_name="external_id_mappings")
    op.drop_index("idx_external_id_mappings_misses", table_name="external_id_mappings")
    op.drop_table("external_id_mappings")
