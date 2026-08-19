from datetime import datetime

from sqlalchemy import DateTime, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UserCalendarCache(Base):
    """Cached episode calendar per user (#194).

    Building the calendar costs two TMDB calls per followed, still-running
    show (show details for next_episode_to_air, then that season's full
    episode list), so the computed payload is cached whole (one row per
    user) and recomputed at most once per TTL or on explicit refresh - see
    routers/calendar.py.
    """

    __tablename__ = "user_calendar_cache"

    id          : Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id     : Mapped[int]      = mapped_column(Integer, nullable=False, unique=True)
    computed_at : Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    payload     : Mapped[dict]     = mapped_column(JSONB, nullable=False, default=dict)
