from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ShowRewatch(Base):
    """Marks that a user is currently rewatching a show. Existence of a row
    is the source of truth for "is this show in an active rewatch cycle" -
    it's deleted (not archived) once the cycle is cancelled or completed."""

    __tablename__ = "show_rewatches"
    __table_args__ = (
        UniqueConstraint("user_id", "show_id", name="uq_show_rewatches_user_show"),
    )

    id         : Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id    : Mapped[int]      = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    show_id    : Mapped[int]      = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class RewatchProgress(Base):
    """One row per episode (re)watched during an active rewatch cycle.
    Tied to the WatchEvent that caused it so deleting that event (unwatch)
    automatically removes the progress row via FK cascade."""

    __tablename__ = "rewatch_progress"
    __table_args__ = (
        UniqueConstraint("rewatch_id", "media_id", name="uq_rewatch_progress_rewatch_media"),
    )

    id             : Mapped[int] = mapped_column(Integer, primary_key=True)
    rewatch_id     : Mapped[int] = mapped_column(ForeignKey("show_rewatches.id", ondelete="CASCADE"), nullable=False, index=True)
    media_id       : Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False, index=True)
    watch_event_id : Mapped[int] = mapped_column(ForeignKey("watch_events.id", ondelete="CASCADE"), nullable=False, index=True)
