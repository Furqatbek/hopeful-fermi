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
from app.platform.errors import Forbidden, Gone, NotFound, RateLimited, Unauthenticated

router = APIRouter(prefix="/auth", tags=["auth"])

OTP_TTL = dt.timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 5
#: Codes one phone may be sent per rolling hour. The budget that protects the
#: number being messaged.
OTP_PER_PHONE_PER_HOUR = 5
#: Codes one client address may request per rolling hour, across ALL numbers.
#: Generous on purpose: in pilot mode a whole prep centre signs in from one
#: classroom NAT, and forty students requesting a code in the same hour must
#: never be refused (the limiter preamble in `api/limits.py` says why a refused
#: student is worse than any abuse). What it caps is a single address sweeping
#: the user base — five sends to every registered phone — which the per-phone
#: budget alone cannot see.
OTP_PER_IP_PER_HOUR = 100

# ── where the refresh token lives ────────────────────────────────────────────
#
# In an httpOnly cookie, not in the response body. ADR-0002 §6 decision 2.
#
# It used to be returned as JSON, and `web/src/api/session.ts` kept it in
# `localStorage` while arguing the trade honestly: "the alternative is an
# httpOnly cookie, which this API cannot set — it is bearer-token throughout,
# and adding a cookie path would mean CSRF defence on every mutating route."
#
# The premise is the part that was wrong. A cookie authenticating EVERY route
# would indeed need CSRF defence everywhere. This one authenticates exactly one
# route — `POST /auth/refresh` — while the access token stays an `Authorization:
# Bearer` header, which a cross-origin page cannot set. So the CSRF surface is a
# single endpoint whose response is unreadable cross-origin, and `SameSite=Strict`
# closes even that: the cookie is not attached to a cross-site request at all.
#
# `Strict` is affordable only because ADR-0002 decision 3 puts the client and the
# API on ONE origin. A fetch from the app's own page to its own `/api/v1/...` is
# same-site, so the cookie rides along; nothing initiated by another site does.
REFRESH_COOKIE = "ielts_refresh"
# Scoped to the auth routes, so the credential is not attached to the hundreds of
# ordinary API calls with no use for it. `/auth/refresh` and `/auth/logout` are
# the only handlers that read or clear it, and both live under this path.
REFRESH_COOKIE_PATH = "/api/v1/auth"


class DeviceInfo(BaseModel):
    """What the client says about itself at sign-in. `DeviceInfo` in the contract.

    Only `label`, because only `label` is stored: it is what `GET /me/devices`
    shows, so a student can tell "Mom's phone" from "Lab PC" when deciding which
    session to forget. The contract also declares `fingerprint` and `platform`;
    a client sending the whole object is not refused — Pydantic drops what is
    not declared — and neither is declared here on purpose, because
    `auth_sessions` has no column for either and a field the model accepts and
    nothing reads is exactly what `check_schema_conformance` refuses to pass.
    `platform` stays the open item its ALLOWED note describes: add the column
    and capture it, or drop it from the schema, but do not invent a value.

    The label is untrusted text rendered in the console, so it is capped.
    """

    label: str | None = Field(default=None, max_length=80)


class TelegramVerify(BaseModel):
    init_data: str | None = None
    contact_phone: str | None = None
    given_name: str | None = None
    date_of_birth: dt.date | None = None
    # The four locales the contract declares and `users.locale` CHECKs.
    locale: str = Field(default="uz-Latn", pattern="^(uz-Latn|uz-Cyrl|ru|en)$")
    device: DeviceInfo | None = None


class OtpRequest(BaseModel):
    phone: str = Field(pattern=PHONE_PATTERN)
    # Both enums as the contract declares them, which is also what the
    # `otp_challenges` CHECKs admit; typed `str` an unknown purpose was a 500
    # from the constraint. `channel` is recorded, not obeyed — see `request_otp`.
    purpose: str = Field(default="login",
                         pattern="^(login|verify_phone|change_phone|recover)$")
    channel: str = Field(default="sms", pattern="^(sms|telegram|voice)$")


class OtpVerify(BaseModel):
    challenge_xid: str
    code: str = Field(pattern=r"^[0-9]{6}$")
    # Both sign-in bodies declare `device` in the contract, and both models
    # silently discarded it — Pydantic drops undeclared fields — so
    # `auth_sessions.device_label` was read by `/me/devices` and written by
    # nothing. Not on `InviteRedeem`: the contract does not declare it there.
    device: DeviceInfo | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _set_refresh_cookie(response: Response, raw: str) -> None:
    """Hand the refresh token to the browser where JavaScript cannot reach it.

    `secure` is off in development and on everywhere else. Browsers do treat
    `localhost` as a trustworthy origin and would accept a `Secure` cookie there,
    but the dev stack serves the console from `localhost:5173` over plain HTTP
    through a Vite proxy, and a flag that *usually* works is the kind of thing
    that costs an afternoon on the one browser where it does not.
    """
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=settings().refresh_token_ttl_days * 24 * 60 * 60,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings().environment != "development",
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    # Same name AND same path. A delete on a different path is a silent no-op
    # that leaves the credential in the browser, and what the client keeps
    # sending is then a token the server already revoked — which reads as
    # "logout is broken" rather than "the delete missed".
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


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


def _open_session(session: Session, user: User, request: Request,
                  response: Response, device: DeviceInfo | None = None) -> dict:
    """Short access JWT in the body + a long opaque refresh token in a cookie.

    Opaque and stored, because a safety ban must kill a live session now and a
    stateless JWT cannot be revoked. 90 days, because every forced re-login is an
    SMS you pay for.

    The refresh token is NOT in the returned dict. It leaves in a `Set-Cookie`
    the page's JavaScript cannot read, so an injected script can steal at most a
    15-minute access token instead of a 90-day session. Every caller passes its
    `Response` — which is why this signature is the only place that had to
    change: all three sign-in paths mint their session here and nowhere else.

    `device` is what the two verify bodies carry; refresh and invite redemption
    pass nothing, and the label stays null for them. A blank label is stored as
    null rather than as `""`, so the devices screen renders its placeholder
    instead of an empty cell.
    """
    raw = secrets.token_urlsafe(48)
    label = device.label.strip() if device and device.label else ""
    row = AuthSession(
        user_id=user.id, token_hash=_hash(raw),
        device_label=label or None,
        user_agent_hash=_hash(request.headers.get("user-agent", ""))[:32],
        expires_at=dt.datetime.now(dt.UTC)
        + dt.timedelta(days=settings().refresh_token_ttl_days))
    session.add(row)
    session.flush()
    _set_refresh_cookie(response, raw)
    return {
        "access_token": issue_access_token(str(user.xid)),
        "expires_in": settings().access_token_ttl_seconds,
        "principal": _principal_dto(session, user),
    }


def _principal_dto(session: Session, user: User) -> dict:
    """The one response every client fetches on every cold start.

    **`memberships` carried `org_id`, an internal bigint, and never carried the
    `org` the contract declares.** Two defects in one field:

      * An internal primary key on the public surface. Every public id in this
        product is an opaque `xid` precisely so that row counts, growth rate and
        enumeration are not inferable from a response — and this handed out the
        raw key of the organizations table.
      * The declared shape was never emitted. `Membership.org` is REQUIRED in
        the contract and resolves to `Org`, so a generated client types
        `memberships[0].org.name` and got `undefined`.

    A client cannot do anything with `1`. It needs the xid to call anything
    org-scoped and the name to render "Tashkent Prep", and the mobile app's home
    screen branches on whether a student belongs to a centre at all.

    Found while generating `docs/api/student-app.md`: the recorded response
    changed between runs because the integer depends on how many rows other
    tests happened to create, which is itself the argument against exposing it.
    """
    from app.modules.identity.models import Organization

    rows = session.execute(
        select(OrgMembership, Organization)
        .join(Organization, Organization.id == OrgMembership.org_id)
        .where(OrgMembership.user_id == user.id,
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
        "memberships": [{"org": {"xid": str(org.xid), "name": org.name,
                                 "slug": org.slug, "kind": org.kind,
                                 "status": org.status},
                         "role": m.role, "status": m.status,
                         "joined_at": iso(m.joined_at)}
                        for m, org in rows],
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


# ── registration by invitation ───────────────────────────────────────────────
#
# **Until this existed, no account could be created at all.** The only code path
# that inserted a `User` was `telegram_verify`, and neither the student app nor
# the console calls it: signing in by code answers "No account exists for this
# number" and accepting an invitation requires already being signed in. A centre
# could be created, a class filled in, a paper published, and not one student
# could get in.
#
# The invitation is the authority. A centre admin with MANAGE_ORG issued it,
# bound to ONE phone number, expiring in fourteen days, stored as a hash. Holding
# it is evidence of nothing on its own — which is why redeeming it still costs a
# one-time code proving that same number, exactly as signing in does. Token and
# phone together are what `telegram_verify` gets from a signed Telegram payload.
#
# ── the delivery gap, said plainly ───────────────────────────────────────────
#
# `otp_request` sends NOTHING to a number with no account: `notify.queue` needs a
# user row, and that silence is deliberate — it is what stops the endpoint being
# a phone-number oracle. In pilot mode the code comes back in the response and
# the screen shows it, so this works today. It is also no worse than sign-in,
# because `transport.sms` has no provider and raises: today the pilot flag is the
# only delivery mechanism that works for ANYONE.
#
# So before `PILOT_OPEN_SIGNIN` is switched off, `notify` must be able to address
# a bare phone number, or an invited student cannot receive their code. That is
# one small change in the notification pipeline and it is named here so it is not
# discovered on the first day of a real intake.


class InvitePreview(BaseModel):
    token: str


class InviteRedeem(BaseModel):
    token: str
    challenge_xid: str
    code: str = Field(pattern=r"^[0-9]{6}$")
    # Only needed when there is no account yet. `users.date_of_birth` is NOT
    # NULL and `adult_at` is generated from it, and every minor rule in the
    # product reads that column — so an account cannot be created without one.
    date_of_birth: dt.date | None = None
    given_name: str | None = None
    locale: str = "uz-Latn"


def _invite_for(session: Session, token: str):
    from sqlalchemy import text

    from app.api.routers.identity import check_invite

    row = session.execute(text("""
        SELECT id, org_id, cohort_id, role, phone, expires_at, accepted_at, revoked_at
        FROM org_invites WHERE token_hash = :h
    """).bindparams(h=_hash(token))).mappings().first()
    # `check_invite` needs a phone to compare against; at preview time the caller
    # has not proven one, so the invite's own is passed and only the
    # revoked/expired/spent checks can fire.
    check_invite(row, row["phone"] if row else "")
    return row


@router.post("/invite/preview")
def invite_preview(body: InvitePreview, session: Session = Depends(db)) -> dict:
    """Who invited you, and what you will be asked for.

    Unauthenticated by necessity — the whole point is that the caller has no
    account — and gated by a secret token, so it is not an enumeration surface.

    **The phone number is masked.** The token names it, but tokens get forwarded,
    and a link that reveals a student's number to whoever opens it is a leak the
    invite flow does not need to take. The invitee types their own number and the
    server checks it matches.
    """
    from app.modules.identity.models import Organization

    row = _invite_for(session, body.token)
    org = session.get(Organization, row["org_id"])
    known = session.scalars(select(User).where(User.phone == row["phone"],
                                               User.deleted_at.is_(None))).first()
    tail = row["phone"][-4:]
    return {
        "org": {"xid": str(org.xid), "name": org.name},
        "role": row["role"],
        "phone_hint": f"•••• {tail}",
        "expires_at": iso(row["expires_at"]),
        # So the screen knows whether to ask for a date of birth. It reveals
        # whether one number is registered, to somebody already holding a valid
        # invite for that number — which the centre admin who issued it knows.
        "needs_account": known is None,
    }


@router.post("/invite/redeem")
def invite_redeem(body: InviteRedeem, request: Request, response: Response,
                  session: Session = Depends(db)) -> dict:
    """Redeem an invitation, creating the account if there is not one yet.

    The order matters. The invite is checked first, so a bad token never charges
    an attempt against somebody's code. Then the code is spent, which is what
    proves the phone. Only then is the phone compared to the invite — a caller
    who proves a DIFFERENT number gets `invite_not_yours`, and the code they
    spent was their own.

    An existing account is not re-registered; it is signed in and given the
    membership. That is the ordinary case of a student at a second centre, and
    it must not look like an error.
    """
    from app.api.routers.identity import check_invite, redeem_invite

    row = _invite_for(session, body.token)
    proven = _consume_challenge(session, body.challenge_xid, body.code)
    # Re-run the full check, now that there is a phone to compare against. The
    # preview above could only test revoked/expired/spent.
    check_invite(row, proven["phone"])

    user = session.scalars(select(User).where(User.phone == proven["phone"],
                                              User.deleted_at.is_(None))).first()
    if user is None:
        if not body.date_of_birth:
            raise Forbidden("A date of birth is required to register.",
                            code="date_of_birth_required")
        user = User(phone=proven["phone"], given_name=body.given_name or "",
                    date_of_birth=body.date_of_birth, locale=body.locale,
                    # Proven in this very request, which is the whole point of
                    # the code — and `accept_invite` requires it of everyone else.
                    phone_verified_at=dt.datetime.now(dt.UTC))
        session.add(user)
        session.flush()
    elif user.phone_verified_at is None:
        user.phone_verified_at = dt.datetime.now(dt.UTC)

    joined = redeem_invite(session, row, user.id)
    return {**_open_session(session, user, request, response), "joined": joined}


@router.post("/telegram/verify")
def telegram_verify(body: TelegramVerify, request: Request, response: Response,
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
        return _open_session(session, user, request, response, body.device)

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
    return _open_session(session, user, request, response, body.device)


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

    # Counted in PostgreSQL, per PHONE and per IP, both fail-closed, and
    # deliberately NOT moved to the Redis limiter in `api/limits.py`. That one
    # fails OPEN so a Redis restart cannot end a student's exam; this one spends
    # real money on every send, so it must be durable and fail closed. Per phone
    # FIRST because the budget protects the number being messaged, and an
    # attacker rotating IPs must not get a fresh allowance for each one.
    oldest, recent = session.execute(text("""
        SELECT min(created_at), count(*) FROM otp_challenges
        WHERE phone = :phone AND created_at > now() - interval '1 hour'
    """).bindparams(phone=body.phone)).one()
    if recent >= OTP_PER_PHONE_PER_HOUR:
        # **429, not 400.** The contract has declared `'429': RateLimited` on this
        # operation from the start and it answered 400 — which every HTTP client
        # treats as a permanent error, so a well-behaved one stops retrying
        # forever and a badly-behaved one is told nothing about when to come back.
        # `Retry-After` is the wait until the oldest of the five ages out.
        wait = int((oldest + dt.timedelta(hours=1)
                    - dt.datetime.now(dt.UTC)).total_seconds())
        raise RateLimited("Too many codes requested for this number.",
                          retry_after=max(1, wait))

    # Per IP second, across every number. ADR-0001 §5.2, the contract's own
    # description of this operation, migration 0003 (`otp_challenges_ip_idx`,
    # built "for per-IP abuse detection") and the compose file all said this
    # existed, and nothing counted `request_ip`: one address could trigger a send
    # to every registered phone, five times an hour each. Skipped when the
    # address does not parse — `"testclient"` here, or whatever a proxy put in a
    # header — the same call the INSERT below makes about the column: the
    # per-phone budget above still holds, and refusing on an address nobody can
    # attribute would refuse everybody behind a misconfigured proxy at once.
    ip = _as_inet(request.client.host if request.client else None)
    if ip is not None:
        oldest, recent = session.execute(text("""
            SELECT min(created_at), count(*) FROM otp_challenges
            WHERE request_ip = CAST(:ip AS inet)
              AND created_at > now() - interval '1 hour'
        """).bindparams(ip=ip)).one()
        if recent >= OTP_PER_IP_PER_HOUR:
            wait = int((oldest + dt.timedelta(hours=1)
                        - dt.datetime.now(dt.UTC)).total_seconds())
            raise RateLimited("Too many codes requested from this address.",
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
                    ip=ip, expires=expires, max_attempts=OTP_MAX_ATTEMPTS))
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


def _consume_challenge(session: Session, challenge_xid, code: str):
    """Charge one attempt against a code, check it, and spend it. Returns the row.

    Extracted from `otp_verify` so invite redemption can prove a phone the SAME
    way rather than a similar way. A second copy of this is a second place for
    the attempt counter, the expiry or the constant-time comparison to drift
    apart — and the one that is wrong is the one nobody is looking at.

    **The charge is committed before the code is checked**, and that commit is
    the one deliberate exception in `app/api` to the request-wide unit of work.
    `platform.db.unit_of_work` rolls back on ANY exception, a `DomainError` on
    its way to a 4xx included, so a refused request leaves no partial write —
    and every wrong guess is refused. So the increment below was rolled back on
    every wrong guess, `attempts` stayed at zero for the life of the challenge,
    and `max_attempts` could never fire: the guard 0011 §11.2 records as fixed
    was fixed only under the test fixture that overrides `deps.db` with a
    session nothing rolls back. Under the real dependency
    (`tests/integration/test_transaction_boundary.py`'s `live_client`) a
    challenge accepted a million guesses in its five minutes.

    The attempt charge IS the write a refused guess must leave behind (ADR-0001
    §5.2, "max 5 attempts"), so it is committed on the request session before
    the checks run. Committed on the request session rather than on a second
    connection, because (a) a second connection cannot see a challenge row the
    request session inserted but has not committed, and would block for ever
    behind a row lock the request session holds — the two ways the suite's
    overridden-session fixture would hang or 401 — and (b) a branch an attacker
    drives by guessing should not cost a second pooled connection per guess.
    The cost is a precondition on callers: nothing may be written to the
    session before this call, or that write is committed too. Both callers
    satisfy it — `otp_verify` calls it first, `invite_redeem` has only read
    `org_invites` — and the rest of the request (consuming the code, opening the
    session) still commits atomically at the request boundary.
    """
    from sqlalchemy import text

    # Charge the attempt to the CHALLENGE, before checking the code. Keyed on the
    # challenge rather than the submitted code, because a wrong code hashes to a
    # row that does not exist: counting only matched rows counts only correct
    # guesses, and the limit could never fire.
    charged = session.execute(text("""
        UPDATE otp_challenges SET attempts = attempts + 1
        WHERE xid = CAST(:x AS uuid) AND consumed_at IS NULL
        RETURNING id, phone, expires_at, attempts, max_attempts, code_hash
    """).bindparams(x=challenge_xid)).mappings().first()
    # Durable before any refusal below can raise — see the docstring.
    session.commit()

    if charged is None:
        # 401, which this operation's contract has always declared. A wrong code
        # is a failed authentication, not a permission denial.
        raise Unauthenticated("That code is not valid.", code="invalid_code")
    if charged["expires_at"] < dt.datetime.now(dt.UTC):
        raise Gone("That code has expired. Request a new one.")
    if charged["attempts"] > charged["max_attempts"]:
        raise Gone("Too many incorrect attempts. Request a new code.")
    if not hmac.compare_digest(
            charged["code_hash"], _hash(f"{challenge_xid}:{code}")):
        raise Unauthenticated("That code is not valid.", code="invalid_code")

    session.execute(text("UPDATE otp_challenges SET consumed_at = now() WHERE id = :id")
                    .bindparams(id=charged["id"]))
    return charged


@router.post("/otp/verify")
def otp_verify(body: OtpVerify, request: Request, response: Response,
               session: Session = Depends(db)) -> dict:

    # Charge the attempt to the CHALLENGE, before checking the code.
    #
    # `max_attempts` was checked below and `attempts` was never incremented
    # anywhere, so the limit could not fire — a challenge accepted unlimited
    # guesses. That is only survivable while `challenge_xid` stays secret, and
    # "the brute-force guard works as long as nothing leaks" is not a guard.
    row = _consume_challenge(session, body.challenge_xid, body.code)

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
    return _open_session(session, user, request, response, body.device)


@router.post("/refresh")
def refresh(request: Request, response: Response,
            session: Session = Depends(db)) -> dict:
    """Rotating. Reuse of an already-rotated token revokes the whole chain —
    that is how a stolen refresh token is detected.

    Takes no request body. The token arrives in the httpOnly cookie the browser
    attaches by itself, so page JavaScript never holds it and cannot send it
    anywhere — which is the whole point of ADR-0002 decision 2.

    The failure branches below deliberately do NOT clear the cookie. They raise,
    and an injected `Response`'s cookies are discarded when an exception handler
    builds the reply instead — so a `_clear_refresh_cookie` there would look like
    it worked and do nothing, which is worse than not writing it. Leaving it
    costs nothing: each of those branches has already revoked the row, so what
    the browser still holds is inert, and the next sign-in overwrites it under
    the same name and path.

    "Has already revoked the row" is true of the reuse branch only because it
    commits its revocation itself, before raising. `unit_of_work` rolls the
    request back on the very 401 that branch answers with, so until it did, the
    chain-wide UPDATE was undone the moment it was issued: the victim's browser
    got `token_reuse_detected` and the thief's rotated token stayed live for the
    rest of its ninety days — the opposite of what the mechanism exists for. The
    suite did not see it because its `client` fixture overrides `deps.db` with a
    session nothing rolls back; `TestUnderTheRealUnitOfWork` in `test_auth.py`
    runs the real dependency and did.
    """
    presented = request.cookies.get(REFRESH_COOKIE)
    if not presented:
        # "You are not signed in", not "your token is bad". Answering
        # `invalid_token` here would send a client that has simply never signed
        # in down the chain-revoked branch of its own error handling.
        raise Unauthenticated("No refresh cookie was presented.", code="no_session")

    row = session.scalars(
        select(AuthSession).where(AuthSession.token_hash == _hash(presented))
    ).first()
    if row is None:
        raise Unauthenticated("Unknown refresh token.", code="invalid_token")
    if row.revoked_at is not None:
        session.execute(
            AuthSession.__table__.update()
            .where(AuthSession.user_id == row.user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="reuse_detected"))
        # Committed here, not at the request boundary: the request transaction
        # is rolled back when this raises (`platform.db.unit_of_work`), and a
        # revocation that rolls back is no revocation. The rule that rollback
        # exists for — "a partial write behind a 4xx is worse than no write" —
        # is about writes a failed operation left half done; this write IS the
        # operation, and the 401 is its report. Nothing else is pending: the
        # only statement before this was a SELECT. On the request session rather
        # than a second connection, for the reasons `_consume_challenge` gives —
        # a thief replaying dead tokens must not cost a pooled connection each,
        # and a second connection would block behind any lock this session held.
        session.commit()
        raise Unauthenticated(
            "This token was already used. All sessions were revoked.",
            code="token_reuse_detected")
    if row.expires_at < dt.datetime.now(dt.UTC):
        raise Unauthenticated("This session has expired.", code="session_expired")

    row.revoked_at = dt.datetime.now(dt.UTC)
    row.revoked_reason = "rotated"
    user = session.get(User, row.user_id)
    opened = _open_session(session, user, request, response)
    session.flush()
    return opened


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(actor: Principal = Depends(principal),
           session: Session = Depends(db)) -> Response:
    session.execute(
        AuthSession.__table__.update()
        .where(AuthSession.user_id == actor.user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="logout"))
    # Cleared on the response this handler actually returns, NOT on an injected
    # one: this builds its own `Response`, and a cookie set on a different object
    # goes nowhere. The rows above are revoked already, so the cookie is inert
    # either way — but a browser that keeps presenting a dead credential earns a
    # 401 on every load until it signs in again.
    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(out)
    return out


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
