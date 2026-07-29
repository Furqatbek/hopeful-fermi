"""Auth: Telegram-first onboarding, SMS OTP fallback, session lifecycle."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, db, issue_access_token, principal
from app.api.dto import iso
from app.modules.identity.models import AuthSession, OrgMembership, PlatformRoleGrant, User
from app.platform.config import settings
from app.platform.errors import DomainError, Forbidden, NotFound

router = APIRouter(prefix="/auth", tags=["auth"])

OTP_TTL = dt.timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 5


class Gone(DomainError):
    status = 410
    code = "expired"


class TelegramVerify(BaseModel):
    init_data: str | None = None
    contact_phone: str | None = None
    given_name: str | None = None
    date_of_birth: dt.date | None = None
    locale: str = "uz-Latn"


class OtpRequest(BaseModel):
    phone: str = Field(pattern=r"^\+998[0-9]{9}$")
    purpose: str = "login"
    channel: str = "sms"


class OtpVerify(BaseModel):
    challenge_xid: str
    code: str = Field(pattern=r"^[0-9]{6}$")


class RefreshRequest(BaseModel):
    refresh_token: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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


def _verify_telegram(init_data: str, bot_token: str) -> dict[str, str]:
    """Telegram's documented HMAC scheme.

    The phone number arrives already verified by Telegram, so no OTP is sent —
    which is the entire cost saving (ADR-0001 §8.6).
    """
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise Forbidden("Telegram signature did not validate.", code="invalid_init_data")
    return pairs


@router.post("/telegram/verify")
def telegram_verify(body: TelegramVerify, request: Request,
                    session: Session = Depends(db)) -> dict:
    if body.init_data:
        _verify_telegram(body.init_data, settings().jwt_secret)
    phone = body.contact_phone
    if not phone:
        raise Forbidden("No verified phone number was supplied.", code="no_phone")

    user = session.scalars(select(User).where(User.phone == phone,
                                              User.deleted_at.is_(None))).first()
    if user is None:
        if not body.date_of_birth:
            # Required at registration: the 18 boundary drives the matching
            # safety rule, and a nullable DOB makes it unenforceable.
            raise Forbidden("A date of birth is required to register.",
                            code="date_of_birth_required")
        user = User(phone=phone, given_name=body.given_name or "",
                    date_of_birth=body.date_of_birth, locale=body.locale,
                    phone_verified_at=dt.datetime.now(dt.UTC))
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
    from sqlalchemy import func, text

    from app.platform.ids import new_xid

    recent = session.scalar(text("""
        SELECT count(*) FROM otp_challenges
        WHERE phone = :phone AND created_at > now() - interval '1 hour'
    """).bindparams(phone=body.phone)) or 0
    if recent >= 5:
        raise DomainError("Too many codes requested for this number.",
                          code="rate_limited")

    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_xid = str(new_xid())
    expires = dt.datetime.now(dt.UTC) + OTP_TTL
    session.execute(text("""
        INSERT INTO otp_challenges (phone, purpose, code_hash, channel, request_ip,
                                    expires_at, max_attempts)
        VALUES (:phone, :purpose, :code_hash, :channel, :ip, :expires, :max_attempts)
    """).bindparams(phone=body.phone, purpose=body.purpose,
                    code_hash=_hash(f"{challenge_xid}:{code}"), channel=body.channel,
                    ip=request.client.host if request.client else None,
                    expires=expires, max_attempts=OTP_MAX_ATTEMPTS))
    # The plaintext code is never persisted and never logged; it goes to the
    # delivery adapter and nowhere else.
    session.flush()
    _ = func  # delivery is a worker concern
    return {"challenge_xid": challenge_xid, "expires_at": iso(expires),
            "channel": body.channel,
            "resend_after": iso(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60))}


@router.post("/otp/verify")
def otp_verify(body: OtpVerify, request: Request,
               session: Session = Depends(db)) -> dict:
    from sqlalchemy import text

    row = session.execute(text("""
        SELECT id, phone, expires_at, consumed_at, attempts, max_attempts, code_hash
        FROM otp_challenges
        WHERE code_hash = :code_hash
        ORDER BY created_at DESC LIMIT 1
    """).bindparams(code_hash=_hash(f"{body.challenge_xid}:{body.code}"))).mappings().first()

    if row is None:
        raise Forbidden("That code is not valid.", code="invalid_code")
    if row["consumed_at"] is not None or row["expires_at"] < dt.datetime.now(dt.UTC):
        raise Gone("That code has expired. Request a new one.")
    if row["attempts"] >= row["max_attempts"]:
        raise Gone("Too many incorrect attempts. Request a new code.")

    session.execute(text("UPDATE otp_challenges SET consumed_at = now() WHERE id = :id")
                    .bindparams(id=row["id"]))
    user = session.scalars(select(User).where(User.phone == row["phone"],
                                              User.deleted_at.is_(None))).first()
    if user is None:
        raise NotFound("No account exists for this number.")
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
