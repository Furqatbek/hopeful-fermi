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

    # Media. `storage_backend` is the portability seam: data residency is a
    # legal question here, and moving to a Tashkent IDC must be a config change
    # rather than a code change (ADR-0001 §5.4).
    storage_backend: str = "file"          # "file" | "s3"
    storage_root: str = "./var/media"      # file backend only
    s3_endpoint: str | None = None
    s3_bucket: str = "ielts-media"
    s3_region: str = "auto"
    s3_access_key: str = ""
    s3_secret_key: str = ""

    # Where the transcode worker unpacks a master before probing it. A 40 MB WAV
    # plus its output on a small writable layer is exactly the surprise that
    # takes a box down at 3 a.m.
    media_scratch_dir: str | None = None

    # "proxy": stream through the app with a per-user grant, full audit, zero CDN
    # cache. "redirect": 302 to a short-TTL presigned URL — much cheaper egress,
    # slightly weaker binding. See docs/design/0009-media.md §4.
    media_delivery: str = "proxy"
    public_base_url: str = "http://localhost:8000"

    # WebRTC. Audio is peer-to-peer and never transits our servers; TURN relays
    # only the connections that cannot be established directly, and its bandwidth
    # is the one line item in this budget that scales with usage
    # (ADR-0001 §7, §5.6).
    stun_url: str = "stun:stun.l.google.com:19302"
    turn_url: str = "turn:turn.example.uz:3478"
    turn_secret: str = "dev-only-change-me"

    # Telegram Mini App. `initData` is HMAC'd with a key derived from the BOT
    # TOKEN, so this is not optional and not interchangeable with `jwt_secret`:
    # signing with our own secret validates data we signed ourselves, which
    # verifies nothing. Empty means the endpoint refuses rather than degrades —
    # `telegram_verify` is an authentication path and it fails closed.
    telegram_bot_token: str = ""
    # Telegram documents rejecting stale initData. A payload harvested from a
    # shared screen or a proxy log stops working within the day.
    telegram_init_data_max_age_seconds: int = 86_400

    # Payment providers. Both callbacks are authenticated with a shared secret
    # rather than an IP allowlist, because a VPS provider's egress addresses are
    # not a security boundary.
    #
    # Empty by default, and both endpoints REFUSE when their secret is empty.
    # These used to default to "dev-only-change-me", which is a secret published
    # in this repository — anyone could compute a valid Click signature or Payme
    # Basic header against a deployment that had not overridden it. A callback
    # that marks orders paid cannot have a default credential.
    payme_merchant_key: str = ""
    click_secret_key: str = ""
    click_service_id: str = "0"

    realtime_url: str = "wss://api.example.uz/realtime"

    environment: str = Field(default="development")

    @property
    def debug(self) -> bool:
        return self.environment == "development"


@lru_cache
def settings() -> Settings:
    return Settings()
