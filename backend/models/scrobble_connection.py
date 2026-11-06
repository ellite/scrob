from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ScrobbleConnection(Base):
    __tablename__ = "scrobble_connections"

    id              : Mapped[int]           = mapped_column(Integer, primary_key=True)
    user_id         : Mapped[int]           = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type            : Mapped[str]           = mapped_column(String(50), nullable=False)   # plex | jellyfin | emby
    name            : Mapped[str]           = mapped_column(String(255), nullable=False)
    server_user_id  : Mapped[Optional[str]] = mapped_column(String(255))  # jellyfin/emby user ID
    server_username : Mapped[Optional[str]] = mapped_column(String(255))  # plex username for webhook attribution
    sync_collection : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True,  server_default="true")
    sync_watched    : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True,  server_default="true")
    sync_playback   : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True,  server_default="true")
    scrobble_percent: Mapped[int]           = mapped_column(Integer, nullable=False, default=75, server_default="75")
    created_at      : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)
