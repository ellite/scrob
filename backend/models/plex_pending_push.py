from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from core.timeutil import utcnow


class PlexPendingPush(Base):
    """One row per (user, media) with a watch pushed to Plex whose echo hasn't
    been seen back yet in a history pull - see GitHub #320. Plex's /:/scrobble
    has no timestamp parameter (it always stamps its own receipt time), so the
    history backfill can't tell a push's echo apart from a genuinely new play
    by comparing against the original watched_at alone, especially when that
    watch was recorded long before it was pushed. Recording our own push time
    here instead gives the backfill something to compare Plex's echo against
    that's actually close to it - just push-to-receipt network latency apart,
    not however old the real watch date is.

    Upserted on every push (one pending row per media, not a growing log) and
    consumed (deleted) by the backfill once matched, or implicitly ignored
    once stale - see PLEX_PENDING_PUSH_MAX_AGE in routers/sync.py.
    """
    __tablename__ = "plex_pending_pushes"
    __table_args__ = (
        UniqueConstraint("user_id", "media_id", name="uq_plex_pending_push_user_media"),
    )

    id        : Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id   : Mapped[int]      = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    media_id  : Mapped[int]      = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    pushed_at : Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
