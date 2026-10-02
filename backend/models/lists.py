from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, PrivacyLevel


class List(Base):
    __tablename__ = "lists"

    id            : Mapped[int]           = mapped_column(Integer, primary_key=True)
    user_id       : Mapped[int]           = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name          : Mapped[str]           = mapped_column(String(255), nullable=False)
    description   : Mapped[Optional[str]] = mapped_column(Text)
    privacy_level : Mapped[PrivacyLevel]  = mapped_column(SQLEnum(PrivacyLevel), default=PrivacyLevel.private, nullable=False, server_default=PrivacyLevel.private.value)
    trakt_slug    : Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mdblist_slug : Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # TMDB's own numeric list id - links a local List to the TMDB list it was
    # imported from, so re-importing the same list adds only new items
    # instead of creating a duplicate local list (#442).
    tmdb_list_id : Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Same idea, for a list imported from TheTVDB (#442 follow-up).
    tvdb_list_id : Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # IMDb list ids have an ``ls`` prefix and are therefore stored as text.
    imdb_list_id : Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # WeTrakr's own numeric list id — links a local List to the remote one it
    # mirrors, set on either a pull (imported from) or a push (created on).
    # BigInteger for the same reason as Comment.wetrakr_comment_id: WeTrakr's
    # own ids for some object types run past Postgres INTEGER's 32-bit range.
    wetrakr_list_id : Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at    : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at    : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user  : Mapped["User"]         = relationship(back_populates="lists")
    items : Mapped[list["ListItem"]] = relationship(back_populates="list", cascade="all, delete-orphan")


class ListItem(Base):
    __tablename__ = "list_items"

    id            : Mapped[int]           = mapped_column(Integer, primary_key=True)
    list_id       : Mapped[int]           = mapped_column(ForeignKey("lists.id", ondelete="CASCADE"), nullable=False)
    media_id      : Mapped[int]           = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    season_number : Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    added_at      : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)
    sort_order    : Mapped[int]           = mapped_column(Integer, default=0, nullable=False)
    notes         : Mapped[Optional[str]] = mapped_column(Text)

    # Unique constraint is a COALESCE expression index (see migration); no SQLAlchemy UniqueConstraint here.

    list  : Mapped["List"]  = relationship(back_populates="items")
    media : Mapped["Media"] = relationship(back_populates="list_items")
