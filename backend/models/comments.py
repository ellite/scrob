from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, Integer, String, Text, Boolean, ForeignKey, DateTime, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        Index("idx_comments_media", "media_type", "tmdb_id", "season_number", "episode_number"),
        Index("idx_comments_tvdb", "media_type", "tvdb_id", "season_number", "episode_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Generic entity referencing
    media_type: Mapped[str] = mapped_column(String(50), nullable=False) # 'movie', 'series', 'season', 'episode', 'person'
    # Exactly one of tmdb_id/tvdb_id is set. For season/episode these are the SHOW's ids;
    # tvdb_id is used only for TVDB-only shows that have no TMDB counterpart.
    tmdb_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tvdb_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    season_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_spoiler: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    # WeTrakr's own comment id — set on pull (imported from) or push (created
    # on). WeTrakr has no edit-comment endpoint and no dedup on write, so this
    # is also what a push checks to avoid re-posting the same comment twice.
    # BigInteger: WeTrakr comment ids run well past Postgres INTEGER's 32-bit
    # range (e.g. 3000053946) — likely a large id-space offset from imported
    # data, not a bug on their side.
    wetrakr_comment_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # WeTrakr's `source` for that comment: "wetrakr" when written there, or the
    # service it was imported from there ("tvtime", "trakt", "letterboxd"...).
    # Shown as the platform credit WeTrakr requires on every comment.
    wetrakr_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    user: Mapped["User"] = relationship()
