from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ConnectionPushState(Base):
    """What a scheduled push last sent to one connection, per item, so the next
    one can skip everything that hasn't changed (#421, #422).

    Without it every auto push re-sent the user's whole rating/watched
    snapshot and repeated a server-side lookup for every item it couldn't
    place - thousands of requests every 15 minutes, which rate-limits Nuvio
    and piles write load onto Plex.

    item_key says what `value` means:
      "rating"            the rating last pushed for the media
      "season_rating:N"   the same for season N of a show
      "watched"           1.0 once the watched flag was pushed (Nuvio: the
                          watched_at, in ms, that was sent)
      "miss" / "miss:..." a server lookup found nothing; `updated_at` is when,
                          and misses are only trusted for a day
    """
    __tablename__ = "connection_push_states"
    __table_args__ = (
        UniqueConstraint("connection_id", "media_id", "item_key", name="uq_connection_push_state_item"),
    )

    id            : Mapped[int]            = mapped_column(Integer, primary_key=True)
    connection_id : Mapped[int]            = mapped_column(ForeignKey("media_server_connections.id", ondelete="CASCADE"), nullable=False)
    media_id      : Mapped[int]            = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    item_key      : Mapped[str]            = mapped_column(String(40), nullable=False)
    value         : Mapped[float | None]   = mapped_column(Float(precision=53), nullable=True)
    updated_at    : Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
