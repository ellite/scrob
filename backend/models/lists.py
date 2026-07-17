from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, Enum as SQLEnum
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
    created_at    : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at    : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user  : Mapped["User"]         = relationship(back_populates="lists")
    items : Mapped[list["ListItem"]] = relationship(back_populates="list", cascade="all, delete-orphan")


class ListItem(Base):
    __tablename__ = "list_items"

    id         : Mapped[int]           = mapped_column(Integer, primary_key=True)
    list_id    : Mapped[int]           = mapped_column(ForeignKey("lists.id", ondelete="CASCADE"), nullable=False)
    media_id   : Mapped[int]           = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    added_at   : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)
    sort_order : Mapped[int]           = mapped_column(Integer, default=0, nullable=False)
    notes      : Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("list_id", "media_id", name="uq_list_item"),
    )

    list  : Mapped["List"]  = relationship(back_populates="items")
    media : Mapped["Media"] = relationship(back_populates="list_items")