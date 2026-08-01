"""Configuration. 12-factor, one `.env`, no provider SDK outside this package."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The values in this file, which are therefore public. Anything still holding one
# outside development is holding a credential printed in a public repository.
PUBLISHED_DEFAULTS = frozenset({"dev-only-change-me", DEV_PLACEHOLDER := (
    "dev-only-not-a-real-secret-change-me-in-production")})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@localhost/ielts"
    redis_url: str = "redis://localhost:6379/0"

    # Short access token, long rotating refresh: every forced re-login is an SMS
    # you pay for (ADR-0001 §5.2).
    #
    # **This defaulted to an 18-character string committed to this repository.**
    # It signs access tokens, and it derives the media-grant key
    # (`platform/grants.py`) and the competition payload key
    # (`routers/competitions.py`) — so a deployment that forgot to set it let
    # anyone who had read the repo mint an access token for any user id,
    # including a platform admin, and open any signed media URL.
    #
    # The argument is already written four fields down, for the payment keys:
    # "a callback that marks orders paid cannot have a default credential."
    # Nothing about that reasoning was specific to payments; this is the same
    # rule applied to the key that authenticates every request in the product.
    # `_no_published_secrets` below is what makes it fail closed.
    #
    # Found by the warnings gate, of all things — PyJWT was emitting
    # `InsecureKeyLengthWarning: the HMAC key is 18 bytes` on every token
    # operation, 4,900 times a run, into output nothing read.
    jwt_secret: str = DEV_PLACEHOLDER
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
    # Also under `_no_published_secrets`. TURN bandwidth is the one line item in
    # this budget that scales with usage, and a published TURN credential is an
    # open relay billed to this project.
    turn_secret: str = DEV_PLACEHOLDER

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

    @model_validator(mode="after")
    def _no_published_secrets(self) -> Settings:
        """Outside development, refuse to start on a secret from this file.

        At import time, not at first use. A signing key checked when a token is
        minted is a check that passes CI, passes a deploy, passes a health probe,
        and fails on the first real request — by which point the process is
        serving. This raises before the application object exists.

        `environment` is the switch rather than a separate flag because it is the
        one setting a deployment cannot forget: nothing else about a non-dev
        deployment works while it says `development`.
        """
        weak = {name: getattr(self, name) for name in ("jwt_secret", "turn_secret")
                if getattr(self, name) in PUBLISHED_DEFAULTS or len(getattr(self, name)) < 32}
        if weak and self.environment != "development":
            raise ValueError(
                f"{', '.join(sorted(weak))}: still set to the value in "
                "app/platform/config.py, or shorter than 32 characters. These are "
                "public — set them from the environment. "
                "`python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
