"""Application configuration loaded from environment variables or .env."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./posts.db"

    jwt_secret: str
    jwt_issuer: str = "otp-service"
    jwt_expire_minutes: int = 15

    access_token_minutes: int = 0
    refresh_token_days: int = 0
    session_persistent_days: int = 3650

    cookie_access_name: str = "app_at"
    cookie_refresh_name: str = "app_rt"
    cookie_domain: str | None = None

    refresh_pepper: str
    base_url: str | None = None
    session_secret: str | None = None
    auth_success_redirect_url: str | None = None

    otp_ttl_seconds: int = 300
    otp_length: int = 6
    otp_daily_limit: int = 10
    otp_request_cooldown_seconds: int = 60
    otp_max_attempts: int = 5
    otp_pepper: str

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    #onesignal通知鈴設定
    onesignal_app_id: str | None = None
    onesignal_rest_api_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("jwt_secret", "refresh_pepper", "otp_pepper")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be set")
        return value


settings = Settings()
