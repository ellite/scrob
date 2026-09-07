"""Source-agnostic media tracking models.

Scrob's ``Media`` model intentionally mirrors TMDB/Jellyfin/Plex playback
objects.  Books and games do not fit that shape, so the tracker keeps a small
canonical catalogue record and a per-user progress record beside (not inside)
the sync domain.  This prevents a new media type from ever changing the
Nuvio/Stremio sync contract.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class CatalogItem(Base):
    __tablename__ = "catalog_items"
    __table_args__ = (
        UniqueConstraint("source", "external_id", "media_type", name="uq_catalog_item_identity"),
        Index("idx_catalog_items_type_title", "media_type", "title"),
    )

    id          : Mapped[int] = mapped_column(Integer, primary_key=True)
    source      : Mapped[str] = mapped_column(String(32), nullable=False)
    external_id : Mapped[str] = mapped_column(String(255), nullable=False)
    media_type  : Mapped[str] = mapped_column(String(32), nullable=False)
    title       : Mapped[str] = mapped_column(String(500), nullable=False)
    original_title: Mapped[Optional[str]] = mapped_column(String(500))
    cover_url   : Mapped[Optional[str]] = mapped_column(String(1000))
    description : Mapped[Optional[str]] = mapped_column(Text)
    release_date: Mapped[Optional[str]] = mapped_column(String(20))
    total_units : Mapped[Optional[int]] = mapped_column(Integer)
    # ``metadata`` is reserved by SQLAlchemy's declarative base, while the
    # database/API column intentionally keeps that familiar name.
    extra_data  : Mapped[Optional[dict]] = mapped_column("metadata", JSONB)
    created_at  : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at  : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    entries: Mapped[list["TrackerEntry"]] = relationship(back_populates="item", cascade="all, delete-orphan")


class TrackerEntry(Base):
    __tablename__ = "tracker_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", name="uq_tracker_entry_user_item"),
        CheckConstraint(
            "status IN ('current', 'completed', 'planning', 'paused', 'dropped', 'repeating')",
            name="ck_tracker_entries_status",
        ),
        CheckConstraint("progress >= 0", name="ck_tracker_entries_progress"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_tracker_entries_score"),
        Index("idx_tracker_entries_user_status", "user_id", "status"),
    )

    id           : Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id      : Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    item_id      : Mapped[int] = mapped_column(ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False)
    status       : Mapped[str] = mapped_column(String(16), nullable=False, default="planning", server_default="planning")
    progress     : Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    score        : Mapped[Optional[float]] = mapped_column(Float)
    notes        : Mapped[Optional[str]] = mapped_column(Text)
    started_at   : Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at : Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at   : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at   : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user : Mapped["User"] = relationship(back_populates="tracker_entries")
    item : Mapped["CatalogItem"] = relationship(back_populates="entries")
