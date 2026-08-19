from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TitleCredits(Base):
    """Cast/crew/studios per title, fetched from TMDB.

    Feeds the people/studio stats on the profile stats page (#124). One row
    per (tmdb_id, media_type), instance-wide (not per user) since credits are
    the same for everyone. Lists are stored as [{"id": int, "name": str}].
    Refreshed in the background at most once per TTL (see core/credits.py).
    """

    __tablename__ = "title_credits"
    __table_args__ = (UniqueConstraint("tmdb_id", "media_type", name="uq_title_credits_title"),)

    id         : Mapped[int]      = mapped_column(Integer, primary_key=True)
    tmdb_id    : Mapped[int]      = mapped_column(Integer, nullable=False, index=True)
    media_type : Mapped[str]      = mapped_column(String(16), nullable=False)  # movie | series
    cast       : Mapped[list]     = mapped_column(JSONB, nullable=False, default=list)
    directors  : Mapped[list]     = mapped_column(JSONB, nullable=False, default=list)
    writers    : Mapped[list]     = mapped_column(JSONB, nullable=False, default=list)
    studios    : Mapped[list]     = mapped_column(JSONB, nullable=False, default=list)
    fetched_at : Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
