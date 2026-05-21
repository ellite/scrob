from typing import Optional

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class GlobalSettings(Base):
    __tablename__ = "global_settings"

    id                     : Mapped[int]           = mapped_column(Integer, primary_key=True)
    tmdb_api_key           : Mapped[Optional[str]] = mapped_column(String(255))
    radarr_url             : Mapped[Optional[str]] = mapped_column(String(500))
    radarr_token           : Mapped[Optional[str]] = mapped_column(String(500))
    radarr_root_folder     : Mapped[Optional[str]] = mapped_column(String(500))
    radarr_quality_profile : Mapped[Optional[int]] = mapped_column(Integer)
    radarr_tags            : Mapped[Optional[list]] = mapped_column(JSONB)
    sonarr_url             : Mapped[Optional[str]] = mapped_column(String(500))
    sonarr_token           : Mapped[Optional[str]] = mapped_column(String(500))
    sonarr_root_folder     : Mapped[Optional[str]] = mapped_column(String(500))
    sonarr_quality_profile : Mapped[Optional[int]] = mapped_column(Integer)
    sonarr_tags            : Mapped[Optional[list]] = mapped_column(JSONB)
    sonarr_season_folder         : Mapped[bool]          = mapped_column(Boolean, nullable=False, server_default="true")
    radarr_require_approval      : Mapped[bool]          = mapped_column(Boolean, nullable=False, server_default="false")
    sonarr_require_approval      : Mapped[bool]          = mapped_column(Boolean, nullable=False, server_default="false")
    tvdb_api_key                 : Mapped[Optional[str]] = mapped_column(String(255))
