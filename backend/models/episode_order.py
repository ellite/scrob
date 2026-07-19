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
    episode_order: Mapped[str] = mapped_column(String(20), nullable=False, default="tmdb")
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
