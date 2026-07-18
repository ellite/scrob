from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MediaServerConnection(Base):
    __tablename__ = "media_server_connections"

    id               : Mapped[int]           = mapped_column(Integer, primary_key=True)
    user_id          : Mapped[int]           = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type             : Mapped[str]           = mapped_column(String(50), nullable=False)   # plex | jellyfin | emby
    name             : Mapped[str]           = mapped_column(String(255), nullable=False)
    url              : Mapped[str]           = mapped_column(String(500), nullable=False)
    token            : Mapped[str]           = mapped_column(String(500), nullable=False)
    server_user_id   : Mapped[Optional[str]] = mapped_column(String(255))  # jellyfin/emby user ID
    server_username  : Mapped[Optional[str]] = mapped_column(String(255))  # plex username for webhook attribution

    # Inbound sync flags (source → Scrob)
    sync_collection  : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,  server_default="true")
    sync_watched     : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,  server_default="true")
    sync_ratings     : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,  server_default="true")
    sync_playback    : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,  server_default="true")

    # Outbound push flags (Scrob → source)
    push_watched     : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    push_playback    : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    push_ratings     : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Auto sync interval in hours (null = disabled)
    auto_sync_interval : Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Plex watchlist → Radarr/Sonarr auto-request (Plex connections only)
    watchlist_to_radarr       : Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    watchlist_to_sonarr       : Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    watchlist_all_users       : Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    watchlist_monitored_users : Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    watchlist_synced_ids      : Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    # Plex watchlist ↔ Scrob list sync (Plex connections only)
    plex_sync_watchlist : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    plex_push_watchlist : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    created_at       : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
