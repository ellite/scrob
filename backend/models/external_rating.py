from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExternalRating(Base):
    """Third-party ratings for one title, as returned by MDBList.

    Shared by all users (ratings are not personal), keyed by TMDB id. One row
    holds every source MDBList returned - IMDb, Rotten Tomatoes Tomatometer
    and Popcornmeter, Metacritic, Letterboxd, Trakt - so switching the
    displayed source in Settings never needs a refetch. ``ratings`` maps a
    Scrob source key (see core/external_ratings.py) to
    ``{"value": float, "votes": int | None, "url": str | None}``; an empty
    dict means MDBList had nothing for the title, which is cached too so it
    isn't asked again on every page view.
    """

    __tablename__ = "external_ratings"
    __table_args__ = (UniqueConstraint("media_type", "tmdb_id", name="uq_external_ratings_title"),)

    id         : Mapped[int]      = mapped_column(Integer, primary_key=True)
    media_type : Mapped[str]      = mapped_column(String(10), nullable=False)  # "movie" | "show"
    tmdb_id    : Mapped[int]      = mapped_column(Integer, nullable=False)
    ratings    : Mapped[dict]     = mapped_column(JSONB, nullable=False, default=dict)
    fetched_at : Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
