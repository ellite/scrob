from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, MediaType


class Media(Base):
    __tablename__ = "media"
    __table_args__ = (
        Index("idx_media_tmdb_type", "tmdb_id", "media_type"),
        Index("idx_media_show_season_episode", "show_id", "season_number", "episode_number"),
    )

    id             : Mapped[int]             = mapped_column(Integer, primary_key=True)
    tmdb_id        : Mapped[Optional[int]]   = mapped_column(Integer)
    media_type     : Mapped[MediaType]       = mapped_column(Enum(MediaType), nullable=False)
    title          : Mapped[str]             = mapped_column(String(500), nullable=False)
    original_title : Mapped[Optional[str]]   = mapped_column(String(500))
    overview       : Mapped[Optional[str]]   = mapped_column(Text)
    poster_path    : Mapped[Optional[str]]   = mapped_column(String(500))
    backdrop_path  : Mapped[Optional[str]]   = mapped_column(String(500))
    release_date   : Mapped[Optional[str]]   = mapped_column(String(20))
    runtime        : Mapped[Optional[int]]   = mapped_column(Integer)
    tmdb_rating    : Mapped[Optional[float]] = mapped_column(Float)
    tagline        : Mapped[Optional[str]]   = mapped_column(Text)
    status         : Mapped[Optional[str]]   = mapped_column(String(100))
    tmdb_data      : Mapped[Optional[dict]]  = mapped_column(JSON)
    adult          : Mapped[bool]            = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Episodes only ↓
    show_id        : Mapped[Optional[int]]   = mapped_column(ForeignKey("shows.id", ondelete="SET NULL"))
    season_number  : Mapped[Optional[int]]   = mapped_column(Integer)
    episode_number : Mapped[Optional[int]]   = mapped_column(Integer)
    created_at     : Mapped[datetime]        = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at     : Mapped[datetime]        = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    show         : Mapped[Optional["Show"]]   = relationship(back_populates="episodes")
    collections  : Mapped[list["Collection"]] = relationship(back_populates="media")
    watch_events : Mapped[list["WatchEvent"]] = relationship(back_populates="media")
    ratings      : Mapped[list["Rating"]]     = relationship(back_populates="media")
    list_items   : Mapped[list["ListItem"]]   = relationship(back_populates="media")
