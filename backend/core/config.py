from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database — accept either a full URL or individual components
    database_url: Optional[str] = None
    postgres_user: Optional[str] = None
    postgres_password: Optional[str] = None
    postgres_db: Optional[str] = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    secret_key: str

    server_url: str = "http://localhost:7330"

    # Set at image build time (see Dockerfile / release workflow) to the actual
    # release version; falls back to "dev" for local/non-release builds.
    app_version: str = "dev"

    # OIDC / SSO
    oidc_enabled: bool = False
    oidc_provider_name: str = "SSO"
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_auth_url: Optional[str] = None
    oidc_token_url: Optional[str] = None
    oidc_userinfo_url: Optional[str] = None
    # OIDC_REDIRECT_URL must point to the frontend /oidc-callback page
    oidc_redirect_url: str = "http://localhost:7330/oidc-callback"
    oidc_logout_url: Optional[str] = None
    oidc_identifier_field: str = "email"
    oidc_scopes: str = "openid email profile"
    oidc_auto_create_users: bool = True
    oidc_disable_password_login: bool = False

    enable_registrations: bool = False
    registration_max_allowed_users: int = 0

    # Trakt.tv
    trakt_client_id: Optional[str] = None
    trakt_client_secret: Optional[str] = None

    require_email_validation: bool = False
    smtp_address: Optional[str] = None
    smtp_port: int = 587
    smtp_encryption: str = "tls"
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    from_email: Optional[str] = None

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        if not all([self.postgres_user, self.postgres_password, self.postgres_db]):
            raise ValueError(
                "Either DATABASE_URL or POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB must be set"
            )
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # File storage root — override with DATA_DIR env var in production / Docker
    data_dir: Path = Path(__file__).parent.parent / "data"

    model_config = {
        "env_file": Path(__file__).parent.parent.parent / ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
