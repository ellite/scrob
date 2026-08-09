from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExternalIdMapping(Base):
    """Global cache of TMDB /find lookups: external id -> TMDB id.

    Deliberately not user-scoped — "tt0903747 is TMDB show 1396" is a fact about
    the world, not about a user, so one resolution serves everybody and survives
    any individual sync's rollback.

    media_kind is part of the key because /find returns movie_results, tv_results
    AND tv_episode_results for a single id, and callers read different buckets.
    Without it, a negative cached for "is this a movie?" would wrongly answer
    "is this a TV show?". For kind "tv_episode_show" the stored tmdb_id is the
    PARENT SHOW's id (tv_episode_results[0].show_id), not the episode's.

    tmdb_id NULL is a negative cache entry — TMDB answered and had no match.
    A failed request is never stored, only a successful empty result.

    Rows are only ever written from live /find responses or an explicit record()
    call with a verified id. Never seeded from Media.tmdb_id, which holds a TVDB
    episode id for TVDB-enriched episodes (see core.enrichment.is_unmapped_tvdb_episode).
    """

    __tablename__ = "external_id_mappings"

    id             : Mapped[int]           = mapped_column(Integer, primary_key=True)
    source         : Mapped[str]           = mapped_column(String(20), nullable=False)   # "imdb_id" | "tvdb_id"
    external_id    : Mapped[str]           = mapped_column(String(64), nullable=False)
    media_kind     : Mapped[str]           = mapped_column(String(20), nullable=False)   # "movie" | "tv" | "tv_episode_show"
    tmdb_id        : Mapped[Optional[int]] = mapped_column(Integer)                      # NULL => negative entry
    miss_count     : Mapped[int]           = mapped_column(Integer, nullable=False, server_default="0")
    checked_at     : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)
    created_at     : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "external_id", "media_kind", name="uq_external_id_mappings_lookup"),
        # Supports a future prune job over stale negatives without scanning hits.
        Index(
            "idx_external_id_mappings_misses",
            "checked_at",
            postgresql_where=text("tmdb_id IS NULL"),
        ),
        # Reverse direction: TMDB id -> external id, used by the outbound push
        # paths and the Sonarr compat shim.
        Index("idx_external_id_mappings_reverse", "source", "media_kind", "tmdb_id"),
    )
