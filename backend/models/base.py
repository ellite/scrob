# app/models/base.py
import enum
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

class UserRole(str, enum.Enum):
    admin = "admin"
    user  = "user"

class MediaType(str, enum.Enum):
    movie   = "movie"
    series  = "series"
    episode = "episode"
    person  = "person"

class CollectionSource(str, enum.Enum):
    jellyfin = "jellyfin"
    emby     = "emby"
    plex     = "plex"
    nuvio    = "nuvio"
    trakt    = "trakt"
    simkl    = "simkl"
    manual   = "manual"
    tmdb     = "tmdb"

class PrivacyLevel(str, enum.Enum):
    public       = "public"
    friends_only = "friends_only"
    private      = "private"