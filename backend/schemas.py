from pydantic import BaseModel, EmailStr, field_validator
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
    is_admin: bool = False
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
    has_effective_tmdb_key: bool = False
    has_global_tmdb_key: bool = False

    # TVDB
    tvdb_api_key: Optional[str] = None
    has_global_tvdb_key: bool = False
    has_effective_tvdb_key: bool = False

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

    # Trakt — app credentials + sync flags; OAuth tokens managed via /trakt/* endpoints
    trakt_client_id: Optional[str] = None
    trakt_client_secret: Optional[str] = None
    trakt_connected: Optional[bool] = None  # read-only, derived from token presence
    trakt_sync_watched: Optional[bool] = None
    trakt_sync_ratings: Optional[bool] = None
    trakt_sync_lists: Optional[bool] = None
    trakt_watchlist_split: Optional[bool] = None
    trakt_push_watched: Optional[bool] = None
    trakt_push_ratings: Optional[bool] = None
    trakt_push_collection: Optional[bool] = None
    trakt_push_lists: Optional[bool] = None
    trakt_scrobble: Optional[bool] = None

    # Simkl — client_id only (PIN flow, no secret); OAuth token managed via /simkl/* endpoints
    simkl_client_id: Optional[str] = None
    simkl_connected: Optional[bool] = None  # read-only, derived from token presence
    simkl_sync_watched: Optional[bool] = None
    simkl_sync_ratings: Optional[bool] = None
    simkl_sync_lists: Optional[bool] = None
    simkl_push_watched: Optional[bool] = None
    simkl_push_ratings: Optional[bool] = None
    simkl_scrobble: Optional[bool] = None

    # MDBList — API key authentication
    mdblist_api_key: Optional[str] = None
    mdblist_connected: Optional[bool] = None  # read-only, validated by /auth/connection-status
    mdblist_sync_watched: Optional[bool] = None
    mdblist_sync_ratings: Optional[bool] = None
    mdblist_sync_watchlist: Optional[bool] = None
    mdblist_push_watched: Optional[bool] = None
    mdblist_push_ratings: Optional[bool] = None
    mdblist_push_watchlist: Optional[bool] = None
    mdblist_push_collection: Optional[bool] = None
    mdblist_scrobble: Optional[bool] = None

    preferences: Optional[dict] = None
    blur_explicit: Optional[bool] = None
    time_format_24h: Optional[bool] = None
    use_hls_player: Optional[bool] = None

    class Config:
        from_attributes = True


class NuvioLoginRequest(BaseModel):
    email: EmailStr
    password: str
    url: str = "https://api.nuvio.tv"


class NuvioConnectionTestRequest(BaseModel):
    url: str
    token: str
    profile_id: int


class MediaServerConnectionBase(BaseModel):
    type: str
    name: str
    url: str
    token: str
    server_user_id: Optional[str] = None
    server_username: Optional[str] = None
    sync_collection: bool = True
    sync_watched: bool = True
    sync_ratings: bool = True
    sync_playback: bool = True
    push_watched: bool = False
    push_playback: bool = False
    push_ratings: bool = False
    auto_sync_interval: Optional[float] = None
    watchlist_to_radarr: bool = False
    watchlist_to_sonarr: bool = False
    watchlist_all_users: bool = False
    watchlist_monitored_users: Optional[list[str]] = None
    plex_sync_watchlist: bool = False
    plex_push_watchlist: bool = False


class MediaServerConnectionCreate(MediaServerConnectionBase):
    pass


class MediaServerConnectionUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    token: Optional[str] = None
    server_user_id: Optional[str] = None
    server_username: Optional[str] = None
    sync_collection: Optional[bool] = None
    sync_watched: Optional[bool] = None
    sync_ratings: Optional[bool] = None
    sync_playback: Optional[bool] = None
    push_watched: Optional[bool] = None
    push_playback: Optional[bool] = None
    push_ratings: Optional[bool] = None
    auto_sync_interval: Optional[float] = None
    watchlist_to_radarr: Optional[bool] = None
    watchlist_to_sonarr: Optional[bool] = None
    watchlist_all_users: Optional[bool] = None
    watchlist_monitored_users: Optional[list[str]] = None
    plex_sync_watchlist: Optional[bool] = None
    plex_push_watchlist: Optional[bool] = None


class MediaServerConnectionResponse(MediaServerConnectionBase):
    id: int
    user_id: int
    created_at: datetime

    class Config:
        from_attributes = True

class ScrobbleConnectionCreate(BaseModel):
    type: str
    name: str
    server_user_id: Optional[str] = None
    server_username: Optional[str] = None
    sync_collection: bool = True
    sync_watched: bool = True
    sync_playback: bool = True


class ScrobbleConnectionUpdate(BaseModel):
    sync_collection: Optional[bool] = None
    sync_watched: Optional[bool] = None
    sync_playback: Optional[bool] = None


class ScrobbleConnectionResponse(ScrobbleConnectionCreate):
    id: int
    user_id: int
    created_at: datetime

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
    series_tmdb_id: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None


class ManualSessionStart(BaseModel):
    tmdb_id: Optional[int] = None
    media_id: Optional[int] = None      # local DB id, preferred over tmdb_id for TVDB-only episodes
    media_type: MediaType
    title: Optional[str] = None
    runtime: Optional[int] = None       # minutes, used if Media.runtime is null
    show_tmdb_id: Optional[int] = None  # episode context
    season_number: Optional[int] = None
    episode_number: Optional[int] = None


class ManualSessionUpdate(BaseModel):
    progress_seconds: int
    state: Optional[str] = None  # "playing" | "paused"


class UserProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    country: Optional[str] = None
    movie_genres: Optional[list[str]] = None
    show_genres: Optional[list[str]] = None
    disliked_genres: Optional[list[str]] = None
    streaming_services: Optional[list[str]] = None
    content_language: Optional[str] = None
    metadata_language: Optional[str] = None
    privacy_level: Optional[PrivacyLevel] = None

class UserProfileResponse(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    country: Optional[str] = None
    movie_genres: list[str] = []
    show_genres: list[str] = []
    disliked_genres: list[str] = []
    streaming_services: list[str] = []
    content_language: Optional[str] = None
    metadata_language: Optional[str] = None
    privacy_level: PrivacyLevel = PrivacyLevel.private
    avatar_url: Optional[str] = None

    @field_validator('movie_genres', 'show_genres', 'disliked_genres', 'streaming_services', mode='before')
    @classmethod
    def _none_to_list(cls, v: object) -> list:
        return v if v is not None else []

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


class GlobalSettings(BaseModel):
    tmdb_api_key           : Optional[str] = None
    tvdb_api_key           : Optional[str] = None
    radarr_url             : Optional[str] = None
    radarr_token           : Optional[str] = None
    radarr_root_folder     : Optional[str] = None
    radarr_quality_profile : Optional[int] = None
    radarr_tags            : Optional[list] = None
    sonarr_url             : Optional[str] = None
    sonarr_token           : Optional[str] = None
    sonarr_root_folder     : Optional[str] = None
    sonarr_quality_profile : Optional[int] = None
    sonarr_tags            : Optional[list] = None
    sonarr_season_folder        : bool = True
    radarr_require_approval     : bool = False
    sonarr_require_approval     : bool = False
    image_cache_enabled         : bool = False
    image_cache_limit_gb        : Optional[int] = None

    class Config:
        from_attributes = True


class MediaRequestOut(BaseModel):
    id          : int
    user_id     : int
    tmdb_id     : int
    media_type  : str
    title       : str
    poster_path : Optional[str]
    status      : str
    reviewed_by : Optional[int]
    created_at  : datetime
    updated_at  : datetime

    class Config:
        from_attributes = True


class AdminUser(BaseModel):
    id         : int
    username   : str
    email      : str
    is_admin   : bool
    api_key    : str
    created_at : datetime

    class Config:
        from_attributes = True
