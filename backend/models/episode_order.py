from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UserShowEpisodeOrder(Base):
    __tablename__ = "user_show_episode_orders"
    __table_args__ = (
        UniqueConstraint("user_id", "series_tmdb_id", name="uq_user_show_episode_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    series_tmdb_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Order key: "tmdb:aired" (default, no row usually), "tvdb:official",
    # "tvdb:dvd", "tvdb:absolute", "tvdb:alternate", "tvdb:regional",
    # "tvdb:type:<id>" (custom/streaming), "tmdb:group:<id>".
    episode_order: Mapped[str] = mapped_column(String(40), nullable=False, default="tmdb:aired")
    # Human label for the selected order, cached so the selector/badges don't
    # need a live provider fetch (e.g. "DVD Order", "Story Arc").
    order_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tvdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class EpisodeOrderMapping(Base):
    __tablename__ = "episode_order_mappings"
    __table_args__ = (
        UniqueConstraint(
            "series_tmdb_id",
            "tmdb_season_number",
            "tmdb_episode_number",
            name="uq_episode_order_mapping_tmdb",
        ),
        UniqueConstraint(
            "series_tmdb_id",
            "tvdb_id",
            name="uq_episode_order_mapping_tvdb_id",
        ),
        Index(
            "idx_episode_order_mapping_tvdb_position",
            "series_tmdb_id",
            "tvdb_season_number",
            "tvdb_episode_number",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_tmdb_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tmdb_season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tmdb_episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tmdb_episode_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tvdb_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tvdb_season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tvdb_episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    match_method: Mapped[str] = mapped_column(String(20), nullable=False, default="external_id")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )


class ShowEpisodePosition(Base):
    """One episode's position in a specific non-aired ordering of a show.

    For a `(series_tmdb_id, order_key)` this table holds the full ordered list:
    `(display_season, display_episode)` is where the episode sits in that
    ordering, and `tmdb_episode_id` (plus the denormalised canonical
    season/episode) points at the one canonical Media row it always maps to.
    Aired order ("tmdb:aired") is the identity mapping and is never stored.

    `core/episode_order.py: build_order_positions` populates this per order
    (delete + reinsert); the show/season/episode endpoints and
    `enrich_with_state` read it to remap what the user sees, while every write
    path translates a display position back to the canonical one before
    touching Media.
    """

    __tablename__ = "show_episode_positions"
    __table_args__ = (
        UniqueConstraint(
            "series_tmdb_id",
            "order_key",
            "display_season",
            "display_episode",
            name="uq_show_episode_position_display",
        ),
        UniqueConstraint(
            "series_tmdb_id",
            "order_key",
            "tmdb_episode_id",
            name="uq_show_episode_position_canonical",
        ),
        Index(
            "idx_show_episode_position_order",
            "series_tmdb_id",
            "order_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_tmdb_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    order_key: Mapped[str] = mapped_column(String(40), nullable=False)
    display_season: Mapped[int] = mapped_column(Integer, nullable=False)
    display_episode: Mapped[int] = mapped_column(Integer, nullable=False)
    tmdb_episode_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tmdb_season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tmdb_episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
