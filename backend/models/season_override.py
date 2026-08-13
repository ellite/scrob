from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class ShowSeasonOverride(Base):
    __tablename__ = "show_season_overrides"
    __table_args__ = (
        UniqueConstraint("user_id", "source_show_tmdb_id", "source_season_number", name="uq_season_override"),
    )

    id                   : Mapped[int]           = mapped_column(Integer, primary_key=True)
    user_id              : Mapped[int]           = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_show_tmdb_id  : Mapped[int]           = mapped_column(Integer, nullable=False)
    source_season_number : Mapped[int]           = mapped_column(Integer, nullable=False)
    # Exactly one of target_show_tmdb_id/target_show_tvdb_id is set (#178) -
    # same either-or convention as Show.tmdb_id/tvdb_id, validated at the API
    # layer rather than a DB constraint.
    target_show_tmdb_id  : Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    target_show_tvdb_id  : Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    target_season_number : Mapped[int]           = mapped_column(Integer, nullable=False)
    created_at           : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)
