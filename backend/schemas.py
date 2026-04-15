from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime
from models.base import UserRole, MediaType, PrivacyLevel

class UserBase(BaseModel):
    email: EmailStr
    username: str
    role: UserRole = UserRole.user

class UserCreate(UserBase):
    password: str

class User(UserBase):
    id: int
    api_key: str
    display_name: str
    totp_enabled: bool = False
    email_confirmed: bool = True
    has_password: bool = True
    created_at: datetime

    class Config:
        from_attributes = True

class UserLogin(BaseModel):
    username: str
    password: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    new_password: str

class Token(BaseModel):
    access_token: Optional[str] = None
    token_type: str = "bearer"
    requires_2fa: bool = False
    temp_token: Optional[str] = None

class TokenPayload(BaseModel):
    sub: Optional[int] = None

class TotpSetupResponse(BaseModel):
    provisioning_uri: str
    secret: str

class TotpEnableRequest(BaseModel):
    secret: str
    code: str

class TotpDisableRequest(BaseModel):
    code: str

class TotpVerifyLoginRequest(BaseModel):
    temp_token: str
    code: str

class TotpBackupCodeItem(BaseModel):
    id: int
    code: str
    used: bool

    class Config:
        from_attributes = True

class TotpBackupCodesResponse(BaseModel):
    codes: list[TotpBackupCodeItem]

class UserSettings(BaseModel):
    tmdb_api_key: Optional[str] = None
    jellyfin_url: Optional[str] = None
    jellyfin_token: Optional[str] = None
    jellyfin_user_id: Optional[str] = None
    emby_url: Optional[str] = None
    emby_token: Optional[str] = None
    emby_user_id: Optional[str] = None
    plex_url: Optional[str] = None
    plex_token: Optional[str] = None
    plex_username: Optional[str] = None

    # Radarr integration
    radarr_url: Optional[str] = None
    radarr_token: Optional[str] = None
    radarr_root_folder: Optional[str] = None
    radarr_quality_profile: Optional[int] = None
    radarr_tags: Optional[list[int]] = None

    # Sonarr integration
    sonarr_url: Optional[str] = None
    sonarr_token: Optional[str] = None
    sonarr_root_folder: Optional[str] = None
    sonarr_quality_profile: Optional[int] = None
    sonarr_tags: Optional[list[int]] = None
    sonarr_season_folder: Optional[bool] = None

    # Inbound sync flags (source → Scrob)
    plex_sync_collection: Optional[bool] = None
    plex_sync_watched: Optional[bool] = None
    plex_sync_ratings: Optional[bool] = None
    plex_sync_playback: Optional[bool] = None
    jellyfin_sync_collection: Optional[bool] = None
    jellyfin_sync_watched: Optional[bool] = None
    jellyfin_sync_ratings: Optional[bool] = None
    jellyfin_sync_playback: Optional[bool] = None
    emby_sync_collection: Optional[bool] = None
    emby_sync_watched: Optional[bool] = None
    emby_sync_ratings: Optional[bool] = None
    emby_sync_playback: Optional[bool] = None

    # Outbound push flags (Scrob → source)
    plex_push_watched: Optional[bool] = None
    plex_push_ratings: Optional[bool] = None
    jellyfin_push_watched: Optional[bool] = None
    jellyfin_push_ratings: Optional[bool] = None
    emby_push_watched: Optional[bool] = None
    emby_push_ratings: Optional[bool] = None

    # Trakt — app credentials + sync flags; OAuth tokens managed via /trakt/* endpoints
    trakt_client_id: Optional[str] = None
    trakt_client_secret: Optional[str] = None
    trakt_connected: Optional[bool] = None  # read-only, derived from token presence
    trakt_sync_watched: Optional[bool] = None
    trakt_sync_ratings: Optional[bool] = None
    trakt_push_watched: Optional[bool] = None
    trakt_push_ratings: Optional[bool] = None

    preferences: Optional[dict] = None

    class Config:
        from_attributes = True

class PasswordUpdate(BaseModel):
    current_password: Optional[str] = None
    new_password: str

class WatchEventCreate(BaseModel):
    tmdb_id: int
    media_type: MediaType
    watched_at: Optional[datetime] = None
    completed: bool = True

class UserProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    country: Optional[str] = None
    movie_genres: Optional[list[str]] = None
    show_genres: Optional[list[str]] = None
    streaming_services: Optional[list[str]] = None
    content_language: Optional[str] = None
    privacy_level: Optional[PrivacyLevel] = None

class UserProfileResponse(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    country: Optional[str] = None
    movie_genres: list[str] = []
    show_genres: list[str] = []
    streaming_services: list[str] = []
    content_language: Optional[str] = None
    privacy_level: PrivacyLevel = PrivacyLevel.private
    avatar_url: Optional[str] = None

    class Config:
        from_attributes = True

class PublicProfileResponse(BaseModel):
    id: int
    username: str
    display_name: str
    bio: Optional[str] = None
    country: Optional[str] = None
    movie_genres: list[str] = []
    show_genres: list[str] = []
    created_at: datetime
    # Stats
    total_watched: int = 0
    total_collected: int = 0
    movies_watched: int = 0
    shows_watched: int = 0
    total_rated: int = 0
    avatar_url: Optional[str] = None
    # Activity
    recently_watched_movies: list[dict] = []
    recently_watched_shows: list[dict] = []
    top_rated_movies: list[dict] = []
    top_rated_shows: list[dict] = []
    recent_comments: list[dict] = []
    lists: list[dict] = []
    follower_count: int = 0
    following_count: int = 0
    followers: list[dict] = []
    following: list[dict] = []
    is_following: bool = False
