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

    # WebRTC. Audio is peer-to-peer and never transits our servers; TURN relays
    # only the connections that cannot be established directly, and its bandwidth
    # is the one line item in this budget that scales with usage
    # (ADR-0001 §7, §5.6).
    stun_url: str = "stun:stun.l.google.com:19302"
    turn_url: str = "turn:turn.example.uz:3478"
    turn_secret: str = "dev-only-change-me"

    # Payment providers. Both callbacks are authenticated with a shared secret
    # rather than an IP allowlist, because a VPS provider's egress addresses are
    # not a security boundary.
    payme_merchant_key: str = "dev-only-change-me"
    click_secret_key: str = "dev-only-change-me"
    click_service_id: str = "0"

    realtime_url: str = "wss://api.example.uz/realtime"

    environment: str = Field(default="development")

    @property
    def debug(self) -> bool:
        return self.environment == "development"


@lru_cache
def settings() -> Settings:
    return Settings()
