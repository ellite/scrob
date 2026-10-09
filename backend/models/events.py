from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func, Index
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
    # When this row was inserted - distinct from watched_at, which is when the
    # user says they watched it (possibly long ago, possibly unknown/NULL).
    # Needed to bound the webhook duplicate-delivery guard for an unknown-dated
    # event (see routers/webhooks.py:_write_watch_event and GitHub #355) -
    # without it, that guard has no way to tell "this null-dated row was just
    # inserted a second ago" from "this null-dated row is three years old",
    # and treating every null-dated event as a permanent duplicate marker
    # silently blocks all future real rewatches of that title.
    created_at       : Mapped[datetime]        = mapped_column(DateTime, server_default=func.now(), nullable=False)
    progress_seconds : Mapped[Optional[int]]   = mapped_column(Integer)
    progress_percent : Mapped[Optional[float]] = mapped_column(Float)
    completed        : Mapped[bool]            = mapped_column(Boolean, default=False, nullable=False)
    play_count       : Mapped[int]             = mapped_column(Integer, default=1, nullable=False)
    # True only for events whose watched_at is an estimate (currently: the Plex
    # webhook's server-receipt time) rather than a timestamp sourced directly
    # from the media server's own record of the play. Lets a later authoritative
    # sync (e.g. _backfill_plex_watch_history) recognize and correct one of
    # these instead of exact-matching against it and creating a duplicate
    # (see GitHub #135).
    provisional      : Mapped[bool]            = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    # Set when the row was pulled from a cloud service ("wetrakr"), so that service's
    # push never sends the play straight back to where it came from.
    origin           : Mapped[Optional[str]]   = mapped_column(String(16), nullable=True)

    user  : Mapped["User"]  = relationship(back_populates="watch_events")
    media : Mapped["Media"] = relationship(back_populates="watch_events")