from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, JSON, String, func, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UserRole


class User(Base):
    __tablename__ = "users"

    id            : Mapped[int]            = mapped_column(Integer, primary_key=True)
    email         : Mapped[str]            = mapped_column(String(255), unique=True, nullable=False)
    username      : Mapped[str]            = mapped_column(String(100), unique=True, nullable=False)
    password_hash : Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)
    api_key       : Mapped[str]            = mapped_column(String(64), unique=True, nullable=False)
    role          : Mapped[UserRole]       = mapped_column(Enum(UserRole), nullable=False, default=UserRole.user)
    email_confirmed : Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    totp_enabled  : Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    totp_secret   : Mapped[Optional[str]]  = mapped_column(String(255))
    created_at    : Mapped[datetime]       = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at    : Mapped[datetime]       = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    @property
    def display_name(self) -> str:
        if self.profile and self.profile.display_name:
            return self.profile.display_name
        return self.username

    @property
    def has_password(self) -> bool:
        return self.password_hash is not None

    settings          : Mapped[Optional["UserSettings"]]   = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    profile           : Mapped[Optional["UserProfileData"]] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    collections       : Mapped[list["Collection"]]         = relationship(back_populates="user", cascade="all, delete-orphan")
    watch_events      : Mapped[list["WatchEvent"]]       = relationship(back_populates="user", cascade="all, delete-orphan")
    ratings           : Mapped[list["Rating"]]           = relationship(back_populates="user", cascade="all, delete-orphan")
    lists             : Mapped[list["List"]]             = relationship(back_populates="user", cascade="all, delete-orphan")
    totp_backup_codes : Mapped[list["TotpBackupCode"]]   = relationship(back_populates="user", cascade="all, delete-orphan")


class AppSettings(Base):
    """Single-row table — global defaults."""
    __tablename__ = "app_settings"

    id               : Mapped[int]            = mapped_column(Integer, primary_key=True)
    jellyfin_url     : Mapped[Optional[str]]  = mapped_column(String(500))
    jellyfin_token   : Mapped[Optional[str]]  = mapped_column(String(500))
    jellyfin_user_id : Mapped[Optional[str]]  = mapped_column(String(255))
    emby_url         : Mapped[Optional[str]]  = mapped_column(String(500))
    emby_token       : Mapped[Optional[str]]  = mapped_column(String(500))
    emby_user_id     : Mapped[Optional[str]]  = mapped_column(String(255))
    plex_url         : Mapped[Optional[str]]  = mapped_column(String(500))
    plex_token       : Mapped[Optional[str]]  = mapped_column(String(500))
    updated_at       : Mapped[datetime]       = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id             : Mapped[int]            = mapped_column(Integer, primary_key=True)
    user_id        : Mapped[int]            = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    tmdb_api_key   : Mapped[Optional[str]]  = mapped_column(String(255))
    jellyfin_url   : Mapped[Optional[str]]  = mapped_column(String(500))
    jellyfin_token : Mapped[Optional[str]]  = mapped_column(String(500))
    jellyfin_user_id : Mapped[Optional[str]]  = mapped_column(String(255))
    emby_url       : Mapped[Optional[str]]  = mapped_column(String(500))
    emby_token     : Mapped[Optional[str]]  = mapped_column(String(500))
    emby_user_id   : Mapped[Optional[str]]  = mapped_column(String(255))
    plex_url       : Mapped[Optional[str]]  = mapped_column(String(500))
    plex_token     : Mapped[Optional[str]]  = mapped_column(String(500))
    plex_username  : Mapped[Optional[str]]  = mapped_column(String(255))
    
    # Radarr integration
    radarr_url             : Mapped[Optional[str]] = mapped_column(String(500))
    radarr_token           : Mapped[Optional[str]] = mapped_column(String(500))
    radarr_root_folder     : Mapped[Optional[str]] = mapped_column(String(500))
    radarr_quality_profile : Mapped[Optional[int]] = mapped_column(Integer)
    radarr_tags            : Mapped[Optional[list[int]]] = mapped_column(JSON)

    # Sonarr integration
    sonarr_url              : Mapped[Optional[str]] = mapped_column(String(500))
    sonarr_token            : Mapped[Optional[str]] = mapped_column(String(500))
    sonarr_root_folder      : Mapped[Optional[str]] = mapped_column(String(500))
    sonarr_quality_profile  : Mapped[Optional[int]] = mapped_column(Integer)
    sonarr_tags             : Mapped[Optional[list[int]]] = mapped_column(JSON)
    sonarr_season_folder    : Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # Inbound sync flags (source → Scrob)
    plex_sync_collection     : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    plex_sync_watched        : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    plex_sync_ratings        : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    plex_sync_playback       : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    jellyfin_sync_collection : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    jellyfin_sync_watched    : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    jellyfin_sync_ratings    : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    jellyfin_sync_playback   : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    emby_sync_collection     : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    emby_sync_watched        : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    emby_sync_ratings        : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    emby_sync_playback       : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    # Outbound push flags (Scrob → source)
    plex_push_watched        : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    plex_push_ratings        : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    jellyfin_push_watched    : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    jellyfin_push_ratings    : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    emby_push_watched        : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    emby_push_ratings        : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Trakt OAuth app credentials (per-user)
    trakt_client_id          : Mapped[Optional[str]]      = mapped_column(String(255))
    trakt_client_secret      : Mapped[Optional[str]]      = mapped_column(String(255))

    # Trakt OAuth tokens
    trakt_access_token       : Mapped[Optional[str]]      = mapped_column(String(2000))
    trakt_refresh_token      : Mapped[Optional[str]]      = mapped_column(String(2000))
    trakt_token_expires_at   : Mapped[Optional[int]]      = mapped_column(BigInteger)  # Unix timestamp
    trakt_device_code        : Mapped[Optional[str]]      = mapped_column(String(255))  # Temporary during device auth

    # Trakt inbound sync flags (Trakt → Scrob)
    trakt_sync_watched       : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    trakt_sync_ratings       : Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    # Trakt outbound push flags (Scrob → Trakt)
    trakt_push_watched       : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    trakt_push_ratings       : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    preferences    : Mapped[Optional[dict]] = mapped_column(JSON)

    user : Mapped["User"] = relationship(back_populates="settings")


class TotpBackupCode(Base):
    __tablename__ = "totp_backup_codes"

    id      : Mapped[int]  = mapped_column(Integer, primary_key=True)
    user_id : Mapped[int]  = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code    : Mapped[str]  = mapped_column(String(20), nullable=False)
    used    : Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user : Mapped["User"] = relationship(back_populates="totp_backup_codes")