from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class WatchEvent(Base):
    __tablename__ = "watch_events"
    __table_args__ = (
        Index("idx_watch_events_user_media", "user_id", "media_id"),
        Index("idx_watch_events_user_completed_watched_at", "user_id", "completed", "watched_at"),
    )

    id               : Mapped[int]             = mapped_column(Integer, primary_key=True)
    user_id          : Mapped[int]             = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    media_id         : Mapped[int]             = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    watched_at       : Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    progress_seconds : Mapped[Optional[int]]   = mapped_column(Integer)
    progress_percent : Mapped[Optional[float]] = mapped_column(Float)
    completed        : Mapped[bool]            = mapped_column(Boolean, default=False, nullable=False)
    play_count       : Mapped[int]             = mapped_column(Integer, default=1, nullable=False)

    user  : Mapped["User"]  = relationship(back_populates="watch_events")
    media : Mapped["Media"] = relationship(back_populates="watch_events")