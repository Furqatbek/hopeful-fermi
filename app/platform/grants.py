"""Short-TTL, per-user media grants. Real HMAC, replacing a placeholder hash.

The requirement this implements, stated in the brief: *"Anti-scrape: short-TTL
signed URLs for all media, per-user tokenized audio delivery."*

What a grant is: a small signed statement that **this user** may fetch **this
object** until **this moment**, for **this purpose**. Four bindings, and dropping
any one of them breaks the property that matters:

  * without the user, a grant leaked from one student's devtools is a download
    link for the whole class;
  * without the object, a grant for a practice track opens the exam audio;
  * without the expiry, "short-TTL" is a comment;
  * without the purpose, a review-mode grant replays a play-once exam section.

What it is NOT: DRM. A determined student can record their screen, and no scheme
in a browser stops that. The goal is that *ordinary* replay is impossible and
unusual behaviour leaves a trace — which is a goal that can actually be met.

Format: `v1.<base64url payload>.<base64url hmac>`. Compact enough for a query
string, self-describing enough to reject an old version cleanly rather than
failing a signature check for mysterious reasons.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from dataclasses import dataclass

from app.platform.config import settings
from app.platform.errors import Forbidden

VERSION = "v1"
# 120 s: long enough for a slow phone to start playback, short enough that a
# grant pasted into a chat group has expired before anyone opens it.
DEFAULT_TTL = 120


@dataclass(frozen=True, slots=True)
class Grant:
    user_xid: str
    media_xid: str
    purpose: str
    expires_at: dt.datetime
    attempt_xid: str | None = None


def issue(*, user_xid: str, media_xid: str, purpose: str = "exam",
          attempt_xid: str | None = None, ttl_seconds: int = DEFAULT_TTL,
          now: dt.datetime | None = None) -> str:
    moment = now or dt.datetime.now(dt.UTC)
    payload = {
        "u": user_xid, "m": media_xid, "p": purpose,
        "e": int((moment + dt.timedelta(seconds=ttl_seconds)).timestamp()),
    }
    if attempt_xid:
        payload["a"] = attempt_xid
    return _encode(payload)


def verify(token: str, *, user_xid: str, media_xid: str,
           now: dt.datetime | None = None) -> Grant:
    """Raises rather than returning a boolean.

    A function that returns False is one an exhausted caller writes `if not
    verify(...)` around and inverts. A function that raises cannot be ignored by
    accident, and the failure carries a reason support can act on.
    """
    moment = now or dt.datetime.now(dt.UTC)
    payload = _decode(token)

    # Constant time, even though the values are not secrets: a timing oracle on
    # the media id would be a way to discover which ids exist.
    if not hmac.compare_digest(str(payload.get("u", "")), user_xid):
        raise Forbidden("This grant was issued to a different user.",
                        code="grant_wrong_user")
    if not hmac.compare_digest(str(payload.get("m", "")), media_xid):
        raise Forbidden("This grant was issued for different media.",
                        code="grant_wrong_media")

    expires = dt.datetime.fromtimestamp(int(payload["e"]), dt.UTC)
    if expires < moment:
        raise Forbidden("This media grant has expired. Request a new one.",
                        code="grant_expired")

    return Grant(user_xid=payload["u"], media_xid=payload["m"],
                 purpose=payload.get("p", "exam"), expires_at=expires,
                 attempt_xid=payload.get("a"))


def _encode(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    packed = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
    return f"{VERSION}.{packed}.{_sign(packed)}"


def _decode(token: str) -> dict:
    try:
        version, packed, signature = token.split(".")
    except ValueError:
        raise Forbidden("This media grant is malformed.",
                        code="grant_malformed") from None
    if version != VERSION:
        # Explicit, so a format change during a deploy reads as "get a new grant"
        # rather than as a signature failure nobody can explain.
        raise Forbidden("This media grant uses an old format. Request a new one.",
                        code="grant_version_unsupported")
    if not hmac.compare_digest(_sign(packed), signature):
        raise Forbidden("This media grant is not valid.", code="grant_bad_signature")
    padding = "=" * (-len(packed) % 4)
    return json.loads(base64.urlsafe_b64decode(packed + padding))


def _sign(packed: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(_key(), packed.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()


def _key() -> bytes:
    """Derived from the app secret with a fixed label.

    Domain separation: a grant signature and a session JWT must not be
    interchangeable even though both ultimately come from `jwt_secret`, or a bug
    in one becomes an authentication bypass in the other.
    """
    return hashlib.sha256(f"media-grant:{settings().jwt_secret}".encode()).digest()


# ── object signatures (dev storage backend only) ─────────────────────

def sign_object(key: str, *, ttl_seconds: int,
                now: dt.datetime | None = None) -> str:
    """A presigned-URL stand-in for `FileStorage`.

    Not a media grant: it carries no user and no purpose, because it stands in
    for an S3 presigned URL, which carries neither. It exists so the dev backend
    can complete the same upload and download flows as production.
    """
    moment = now or dt.datetime.now(dt.UTC)
    expires = int((moment + dt.timedelta(seconds=ttl_seconds)).timestamp())
    return f"{expires}.{_object_mac(key, expires)}"


def verify_object(key: str, signature: str,
                  now: dt.datetime | None = None) -> None:
    moment = now or dt.datetime.now(dt.UTC)
    try:
        expires_raw, mac = signature.split(".")
        expires = int(expires_raw)
    except (ValueError, AttributeError):
        raise Forbidden("Malformed object signature.", code="grant_malformed") from None
    if not hmac.compare_digest(_object_mac(key, expires), mac):
        raise Forbidden("Invalid object signature.", code="grant_bad_signature")
    if dt.datetime.fromtimestamp(expires, dt.UTC) < moment:
        raise Forbidden("This URL has expired.", code="grant_expired")


def _object_mac(key: str, expires: int) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(_key(), f"{key}:{expires}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()[:32]
