from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


RatingKey = tuple[int, int | None]
RatingChanges = dict[RatingKey, float]


class Rating(Base):
    __tablename__ = "ratings"

    id            : Mapped[int]             = mapped_column(Integer, primary_key=True)
    user_id       : Mapped[int]             = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    media_id      : Mapped[int]             = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    season_number : Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    rating        : Mapped[Optional[float]] = mapped_column(Float)
    review        : Mapped[Optional[str]]   = mapped_column(Text)
    rated_at      : Mapped[datetime]        = mapped_column(DateTime, server_default=func.now(), nullable=False)

    # Unique constraint is a COALESCE expression index (see migration); no SQLAlchemy UniqueConstraint here.

    user  : Mapped["User"]  = relationship(back_populates="ratings")
    media : Mapped["Media"] = relationship(back_populates="ratings")