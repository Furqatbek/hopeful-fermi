"""Auth: Telegram-first onboarding, SMS OTP fallback, session lifecycle."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, db, issue_access_token, principal
from app.api.dto import iso
from app.modules.identity.models import (
    PHONE_PATTERN,
    AuthSession,
    OrgMembership,
    PlatformRoleGrant,
    User,
)
from app.platform.config import settings
from app.platform.errors import Forbidden, Gone, NotFound, RateLimited

router = APIRouter(prefix="/auth", tags=["auth"])

OTP_TTL = dt.timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 5


class TelegramVerify(BaseModel):
    init_data: str | None = None
    contact_phone: str | None = None
    given_name: str | None = None
    date_of_birth: dt.date | None = None
    locale: str = "uz-Latn"


class OtpRequest(BaseModel):
    phone: str = Field(pattern=PHONE_PATTERN)
    purpose: str = "login"
    channel: str = "sms"


class OtpVerify(BaseModel):
    challenge_xid: str
    code: str = Field(pattern=r"^[0-9]{6}$")


class RefreshRequest(BaseModel):
    refresh_token: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _as_inet(value: str | None) -> str | None:
    """`request_ip` is `inet`, and `request.client.host` is `"testclient"` under
    the test client and whatever a proxy puts in a header in production. Same
    fix as `content_attestations.ip` in `0009` §6.4: the address is context, the
    challenge is the point, so drop it rather than lose the row."""
    import ipaddress

    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _open_session(session: Session, user: User, request: Request) -> dict:
    """Short access JWT + a long opaque refresh token.

    Opaque and stored, because a safety ban must kill a live session now and a
    stateless JWT cannot be revoked. 90 days, because every forced re-login is an
    SMS you pay for.
    """
    raw = secrets.token_urlsafe(48)
    row = AuthSession(
        user_id=user.id, token_hash=_hash(raw),
        user_agent_hash=_hash(request.headers.get("user-agent", ""))[:32],
        expires_at=dt.datetime.now(dt.UTC)
        + dt.timedelta(days=settings().refresh_token_ttl_days))
    session.add(row)
    session.flush()
    return {
        "access_token": issue_access_token(str(user.xid)),
        "refresh_token": raw,
        "expires_in": settings().access_token_ttl_seconds,
        "principal": _principal_dto(session, user),
    }


def _principal_dto(session: Session, user: User) -> dict:
    memberships = session.scalars(
        select(OrgMembership).where(OrgMembership.user_id == user.id,
                                    OrgMembership.status == "active")).all()
    roles = session.scalars(
        select(PlatformRoleGrant.role).where(PlatformRoleGrant.user_id == user.id,
                                             PlatformRoleGrant.revoked_at.is_(None))).all()
    return {
        "user": {"xid": str(user.xid), "phone": user.phone,
                 "given_name": user.given_name, "family_name": user.family_name,
                 "locale": user.locale, "timezone": user.timezone,
                 "telegram_username": user.telegram_username,
                 "target_band": float(user.target_band) if user.target_band else None,
                 # `date_of_birth` is deliberately absent from every response.
                 "is_minor": user.adult_at > dt.datetime.now(dt.UTC).date()},
        "memberships": [{"org_id": m.org_id, "role": m.role, "status": m.status}
                        for m in memberships],
        "platform_roles": list(roles),
        "is_minor": user.adult_at > dt.datetime.now(dt.UTC).date(),
        "server_now": iso(dt.datetime.now(dt.UTC)),
    }


def _verify_telegram(init_data: str, bot_token: str,
                     now: dt.datetime | None = None) -> dict[str, str]:
    """Telegram's documented HMAC scheme, verified against the BOT TOKEN.

    Signing with our own `jwt_secret` — which this did — validates data we signed
    ourselves and therefore verifies nothing at all.

    Fails closed when no bot token is configured. An authentication path that
    degrades to "allow" when a secret is missing is worse than one that is simply
    turned off, because the missing secret is invisible until someone looks.
    """
    if not bot_token:
        raise Forbidden("Telegram sign-in is not configured on this server.",
                        code="telegram_not_configured")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise Forbidden("Telegram signature did not validate.", code="invalid_init_data")

    # Replay window. Telegram documents rejecting stale `auth_date`, and without
    # it one captured initData string is a permanent credential.
    moment = now or dt.datetime.now(dt.UTC)
    try:
        issued = dt.datetime.fromtimestamp(int(pairs.get("auth_date", "")), dt.UTC)
    except (TypeError, ValueError):
        raise Forbidden("Telegram payload has no usable auth_date.",
                        code="invalid_init_data") from None
    age = (moment - issued).total_seconds()
    if age > settings().telegram_init_data_max_age_seconds or age < -300:
        raise Forbidden("This Telegram sign-in has expired. Open the app again.",
                        code="init_data_expired")
    return pairs


def _telegram_identity(pairs: dict[str, str]) -> tuple[int, str | None]:
    """The `user` object Telegram signs. This, and not the request body, is who
    the caller is."""
    import json

    try:
        user = json.loads(pairs.get("user", ""))
        return int(user["id"]), user.get("username")
    except (ValueError, KeyError, TypeError):
        raise Forbidden("Telegram payload carried no user.",
                        code="invalid_init_data") from None


@router.post("/telegram/verify")
def telegram_verify(body: TelegramVerify, request: Request,
                    session: Session = Depends(db)) -> dict:
    """Identity comes from the SIGNED payload, never from the request body.

    This previously accepted `contact_phone` on its own — `init_data` was
    optional, and when absent nothing was verified. POSTing a phone number
    returned a working access token for that account: a complete authentication
    bypass against any user whose number you knew, including minors.

    So: `init_data` is required, and the account is keyed on the Telegram user id
    inside it. `contact_phone` is untrusted input — Mini App `initData` does not
    carry a phone number, it comes from `requestContact` in the client and a
    client can send anything. It may therefore REGISTER a number, never claim
    one: if it matches an account that already exists, the answer is to prove
    ownership by OTP, because otherwise the bypass is back with one extra step.
    """
    if not body.init_data:
        raise Forbidden("Telegram initData is required.", code="init_data_required")
    pairs = _verify_telegram(body.init_data, settings().telegram_bot_token)
    telegram_id, username = _telegram_identity(pairs)

    user = session.scalars(select(User).where(User.telegram_user_id == telegram_id,
                                              User.deleted_at.is_(None))).first()
    if user is not None:
        if username and user.telegram_username != username:
            user.telegram_username = username
        return _open_session(session, user, request)

    phone = body.contact_phone
    if not phone:
        raise Forbidden("No phone number was supplied.", code="no_phone")

    existing = session.scalars(select(User).where(User.phone == phone,
                                                  User.deleted_at.is_(None))).first()
    if existing is not None:
        # The takeover vector, closed. Linking a Telegram account to a number
        # that already has an account is a claim about the phone, and only an OTP
        # proves it.
        raise Forbidden(
            "An account already uses this number. Sign in by SMS code to link "
            "your Telegram account to it.", code="phone_already_registered")

    if not body.date_of_birth:
        # Required at registration: the 18 boundary drives the matching safety
        # rule, and a nullable DOB makes it unenforceable.
        raise Forbidden("A date of birth is required to register.",
                        code="date_of_birth_required")
    user = User(phone=phone, given_name=body.given_name or "",
                date_of_birth=body.date_of_birth, locale=body.locale,
                telegram_user_id=telegram_id, telegram_username=username,
                # NOT verified. Telegram vouched for the Telegram account, not
                # for this number; `phone_verified_at` is set by the OTP path.
                phone_verified_at=None)
    session.add(user)
    session.flush()
    return _open_session(session, user, request)


@router.post("/otp/request", status_code=status.HTTP_202_ACCEPTED)
def otp_request(body: OtpRequest, request: Request,
                session: Session = Depends(db)) -> dict:
    """Always 202, whether or not the number exists.

    Anything else turns this into a phone-number oracle. It also spends real
    money per call, so the rate limits here are a budget control as much as an
    abuse control.
    """
    from sqlalchemy import text

    from app.modules.identity import notify
    from app.platform.ids import new_xid

    # Counted in PostgreSQL, per PHONE, and deliberately NOT moved to the Redis
    # limiter in `api/limits.py`. That one fails OPEN so a Redis restart cannot
    # end a student's exam; this one spends real money on every send, so it must
    # be durable and fail closed. Per phone rather than per caller for the same
    # reason: the budget protects the number being messaged, and an attacker
    # rotating IPs must not get a fresh allowance for each one.
    oldest, recent = session.execute(text("""
        SELECT min(created_at), count(*) FROM otp_challenges
        WHERE phone = :phone AND created_at > now() - interval '1 hour'
    """).bindparams(phone=body.phone)).one()
    if recent >= 5:
        # **429, not 400.** The contract has declared `'429': RateLimited` on this
        # operation from the start and it answered 400 — which every HTTP client
        # treats as a permanent error, so a well-behaved one stops retrying
        # forever and a badly-behaved one is told nothing about when to come back.
        # `Retry-After` is the wait until the oldest of the five ages out.
        wait = int((oldest + dt.timedelta(hours=1)
                    - dt.datetime.now(dt.UTC)).total_seconds())
        raise RateLimited("Too many codes requested for this number.",
                          retry_after=max(1, wait))

    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_xid = str(new_xid())
    expires = dt.datetime.now(dt.UTC) + OTP_TTL
    session.execute(text("""
        INSERT INTO otp_challenges (xid, phone, purpose, code_hash, channel, request_ip,
                                    expires_at, max_attempts)
        VALUES (CAST(:xid AS uuid), :phone, :purpose, :code_hash, :channel,
                CAST(:ip AS inet), :expires, :max_attempts)
    """).bindparams(xid=challenge_xid, phone=body.phone, purpose=body.purpose,
                    code_hash=_hash(f"{challenge_xid}:{code}"), channel=body.channel,
                    ip=_as_inet(request.client.host if request.client else None),
                    expires=expires, max_attempts=OTP_MAX_ATTEMPTS))
    # **The code was generated, hashed into the row above, and dropped.** Nothing
    # sent it: the comment here described a delivery adapter that did not exist
    # and `_ = func` stood in for the call. So no login code had ever reached a
    # phone, which made SMS and Telegram sign-in unreachable — and with it
    # `phone_verified_at`, which `routers/identity.py` requires before a student
    # may accept an invitation to a prep centre.
    #
    # Queued, not sent inline: delivery is a worker concern, and a login endpoint
    # that blocks on api.telegram.org answers as slowly as Telegram does.
    #
    # The code therefore travels through `notifications.params`, because that
    # table is the only channel between this handler and the worker. That is the
    # one place a plaintext code is persisted — `otp_challenges` keeps only a
    # hash — and `notify.deliver` strips it (`notify.SECRET_PARAMS`) the moment
    # the row is `sent` or `failed`. It is never logged, and never returned.
    #
    # Silence for a number with no account is what keeps this from being a
    # phone-number oracle: `notify.queue` needs a user, an unknown number has
    # none, and the response is the same 202 either way.
    user = session.scalars(select(User).where(User.phone == body.phone,
                                              User.deleted_at.is_(None))).first()
    if user is not None:
        # `channel` is NOT taken from the request. Which channel a message costs
        # money on is a budget decision that `notify._channel` owns — it picks
        # free Telegram whenever the account is linked — and letting a client
        # name it would let anyone force the paid one.
        notify.queue(session, user_id=user.id, template="auth.otp",
                     params={"code": code})
    session.flush()
    return {"challenge_xid": challenge_xid, "expires_at": iso(expires),
            # What was ASKED for, not what was picked — unchanged, and worth
            # naming so nobody reads it as the delivery channel. The contract's
            # enum is [sms, telegram, voice] and `_channel` can answer `in_app`,
            # so reporting the real one is a contract change, not a code change.
            "channel": body.channel,
            "resend_after": iso(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60)),
            **_pilot_code(session, body.phone, code, request)}


def _pilot_code(session, phone: str, code: str, request: Request) -> dict:
    """The pilot escape hatch, and it is authentication switched off.

    There is no SMS provider — `identity/transport.py` fails closed rather than
    pretending — so a code reaches an account only over Telegram. A first prep
    centre is forty students with no Telegram link and no way in, which is why
    `pilot_open_signin` exists.

    What it costs, said plainly: anyone who knows a phone number can sign in as
    that person. Nothing here narrows it, because narrowing it while calling it
    open would be the dangerous kind of half-measure — an org-scoped lookup a
    centre admin runs for their own roster is the shape to build if this
    outlives the pilot.

    Every issuance is audited and logged. "Was it on, and for how long" must be
    answerable from the database rather than from somebody's memory of a deploy.
    """
    import structlog
    from sqlalchemy import text

    if not settings().pilot_open_signin:
        return {}
    structlog.get_logger().warning("pilot_open_signin_code_issued", phone=phone)
    session.execute(text("""
        INSERT INTO audit_log (actor_kind, action, subject_type, subject_id, after)
        VALUES ('system', 'auth.pilot_code_issued', 'phone', :phone,
                CAST(:after AS jsonb))
    """).bindparams(phone=phone, after=json.dumps({
        "phone": phone,
        "ip": request.client.host if request.client else None,
    })))
    return {"pilot_code": code}


@router.post("/otp/verify")
def otp_verify(body: OtpVerify, request: Request,
               session: Session = Depends(db)) -> dict:
    from sqlalchemy import text

    # Charge the attempt to the CHALLENGE, before checking the code.
    #
    # `max_attempts` was checked below and `attempts` was never incremented
    # anywhere, so the limit could not fire — a challenge accepted unlimited
    # guesses. That is only survivable while `challenge_xid` stays secret, and
    # "the brute-force guard works as long as nothing leaks" is not a guard.
    #
    # It has to be keyed on the challenge rather than on the submitted code,
    # because a wrong code hashes to a row that does not exist: counting only
    # matched rows counts only correct guesses.
    charged = session.execute(text("""
        UPDATE otp_challenges SET attempts = attempts + 1
        WHERE xid = CAST(:x AS uuid) AND consumed_at IS NULL
        RETURNING id, phone, expires_at, attempts, max_attempts, code_hash
    """).bindparams(x=body.challenge_xid)).mappings().first()

    if charged is None:
        raise Forbidden("That code is not valid.", code="invalid_code")
    if charged["expires_at"] < dt.datetime.now(dt.UTC):
        raise Gone("That code has expired. Request a new one.")
    if charged["attempts"] > charged["max_attempts"]:
        raise Gone("Too many incorrect attempts. Request a new code.")
    if not hmac.compare_digest(
            charged["code_hash"], _hash(f"{body.challenge_xid}:{body.code}")):
        raise Forbidden("That code is not valid.", code="invalid_code")
    row = charged

    session.execute(text("UPDATE otp_challenges SET consumed_at = now() WHERE id = :id")
                    .bindparams(id=row["id"]))
    user = session.scalars(select(User).where(User.phone == row["phone"],
                                              User.deleted_at.is_(None))).first()
    if user is None:
        raise NotFound("No account exists for this number.")

    # **This is the only place a phone number is ever proven, and it was not
    # being recorded.** `telegram_verify` sets `phone_verified_at=None` with the
    # comment "`phone_verified_at` is set by the OTP path" — and the OTP path did
    # not set it, so the column was written `None` and read by nothing.
    #
    # It matters now because an invite is bound to a phone number. Registration
    # takes the number from `requestContact`, which the client controls, so
    # `users.phone` on its own is a self-declared string: matching an invite
    # against it would be a lock whose key is "type the number you want". A code
    # delivered to the handset is what makes it a fact.
    if user.phone_verified_at is None:
        user.phone_verified_at = dt.datetime.now(dt.UTC)
    return _open_session(session, user, request)


@router.post("/refresh")
def refresh(body: RefreshRequest, request: Request,
            session: Session = Depends(db)) -> dict:
    """Rotating. Reuse of an already-rotated token revokes the whole chain —
    that is how a stolen refresh token is detected."""
    row = session.scalars(
        select(AuthSession).where(AuthSession.token_hash == _hash(body.refresh_token))
    ).first()
    if row is None:
        raise Forbidden("Unknown refresh token.", code="invalid_token")
    if row.revoked_at is not None:
        session.execute(
            AuthSession.__table__.update()
            .where(AuthSession.user_id == row.user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="reuse_detected"))
        raise Forbidden("This token was already used. All sessions were revoked.",
                        code="token_reuse_detected")
    if row.expires_at < dt.datetime.now(dt.UTC):
        raise Forbidden("This session has expired.", code="session_expired")

    row.revoked_at = dt.datetime.now(dt.UTC)
    row.revoked_reason = "rotated"
    user = session.get(User, row.user_id)
    opened = _open_session(session, user, request)
    session.flush()
    return opened


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(actor: Principal = Depends(principal),
           session: Session = Depends(db)) -> Response:
    session.execute(
        AuthSession.__table__.update()
        .where(AuthSession.user_id == actor.user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="logout"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/session")
def read_session(actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> dict:
    """Roles, memberships and entitlements in one call, so the client renders
    navigation without N permission probes."""
    from app.modules.billing.models import EntitlementRow

    user = session.get(User, actor.user_id)
    dto = _principal_dto(session, user)
    dto["entitlements"] = [
        {"feature": e.feature, "subject_kind": e.subject_kind,
         "source_kind": e.source_kind, "quantity": e.quantity,
         "remaining": None if e.quantity is None else max(0, e.quantity - e.consumed),
         "starts_at": iso(e.starts_at), "expires_at": iso(e.expires_at)}
        for e in session.scalars(
            select(EntitlementRow).where(EntitlementRow.revoked_at.is_(None),
                                         EntitlementRow.subject_kind == "user",
                                         EntitlementRow.subject_id == actor.user_id))
    ]
    return dto
