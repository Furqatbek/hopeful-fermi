"""Configuration. 12-factor, one `.env`, no provider SDK outside this package."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@localhost/ielts"
    redis_url: str = "redis://localhost:6379/0"

    # Short access token, long rotating refresh: every forced re-login is an SMS
    # you pay for (ADR-0001 §5.2).
    jwt_secret: str = "dev-only-change-me"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_days: int = 90

    # Absorbs a mobile network hiccup at the deadline without letting anyone
    # meaningfully overrun. Recorded in `attempts.late_by_ms` either way.
    submit_grace_seconds: int = 30
    media_grant_ttl_seconds: int = 120

    s3_endpoint: str | None = None
    s3_bucket: str = "ielts-media"
    s3_region: str = "auto"

    environment: str = Field(default="development")

    @property
    def debug(self) -> bool:
        return self.environment == "development"


@lru_cache
def settings() -> Settings:
    return Settings()
