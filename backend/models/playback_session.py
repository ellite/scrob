from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Index
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from core.timeutil import utcnow


class PlaybackSession(Base):
    __tablename__ = "playback_sessions"
    __table_args__ = (
        Index("idx_playback_sessions_session_key", "session_key"),
    )

    id               : Mapped[int]            = mapped_column(Integer, primary_key=True)
    user_id          : Mapped[int]            = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    media_id         : Mapped[int]            = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    session_key      : Mapped[str]            = mapped_column(String(255), nullable=False, unique=True)
    source           : Mapped[str]            = mapped_column(String(16), nullable=False)  # "plex" | "jellyfin"
    state            : Mapped[str]            = mapped_column(String(16), default="playing", nullable=False)  # "playing" | "paused"
    progress_percent : Mapped[float]          = mapped_column(Float, default=0.0, nullable=False)
    progress_seconds : Mapped[int]            = mapped_column(Integer, default=0, nullable=False)
    started_at       : Mapped[datetime]       = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at       : Mapped[datetime]       = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
