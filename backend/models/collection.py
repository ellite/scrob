from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, CollectionSource
from .connections import MediaServerConnection


class Collection(Base):
    __tablename__ = "collections"

    id       : Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id  : Mapped[int]      = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    media_id : Mapped[int]      = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    added_at : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "media_id", name="uq_collection_user_media"),
    )

    user  : Mapped["User"]                  = relationship(back_populates="collections")
    media : Mapped["Media"]                 = relationship(back_populates="collections")
    files : Mapped[list["CollectionFile"]]  = relationship(back_populates="collection", cascade="all, delete-orphan")


class CollectionFile(Base):
    """One row per (collection, source, source_id) — a physical file or source entry.

    A Collection (one per user+media) can have multiple CollectionFiles when the same item
    exists in several sources (e.g., both Plex and Jellyfin).
    """
    __tablename__ = "collection_files"

    id                 : Mapped[int]               = mapped_column(Integer, primary_key=True)
    collection_id      : Mapped[int]               = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), nullable=False)
    connection_id      : Mapped[Optional[int]]     = mapped_column(ForeignKey("media_server_connections.id", ondelete="SET NULL"), nullable=True)
    source             : Mapped[CollectionSource]  = mapped_column(Enum(CollectionSource), nullable=False)
    source_id          : Mapped[Optional[str]]     = mapped_column(String(255))
    resolution         : Mapped[Optional[str]]     = mapped_column(String(50))
    video_codec        : Mapped[Optional[str]]     = mapped_column(String(50))
    audio_codec        : Mapped[Optional[str]]     = mapped_column(String(50))
    audio_channels     : Mapped[Optional[str]]     = mapped_column(String(20))
    audio_languages    : Mapped[Optional[list]]    = mapped_column(JSON)
    subtitle_languages : Mapped[Optional[list]]    = mapped_column(JSON)
    file_path          : Mapped[Optional[str]]     = mapped_column(String(1000))
    added_at           : Mapped[datetime]          = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("collection_id", "source", "source_id", name="uq_collection_file_source"),
    )

    collection : Mapped["Collection"]              = relationship(back_populates="files")
    connection : Mapped[Optional["MediaServerConnection"]] = relationship()
