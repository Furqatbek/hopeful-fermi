"""Request-scoped dependencies: principal, unit of work, services, idempotency."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from fastapi import Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.billing.entitlements import Entitlements, EntitlementStore
from app.modules.exam.models import IdempotencyKey
from app.modules.exam.session import ExamSession
from app.modules.qtypes.registry import (
    Registry, Scorer, default_registry, default_scorer,
)
from app.platform.clock import Clock, SystemClock
from app.platform.config import settings
from app.platform.db import session_factory
from app.platform.errors import Conflict, Forbidden, NotFound

ROOT = Path(__file__).resolve().parents[2]


def registry() -> Registry:
    """One process-wide instance, owned by the qtypes module.

    Cached there rather than here so the worker pool shares it: two caches of a
    few hundred kilobytes is not the problem — two code paths that could load
    different definitions is.
    """
    return default_registry()


def scorer() -> Scorer:
    return default_scorer()


def clock() -> Clock:
    return SystemClock()


def db() -> Iterator[Session]:
    """One transaction per request. Committed on success, rolled back on any
    exception — including a DomainError that becomes a 4xx, because a partial
    write behind a 409 is worse than no write."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    user_xid: str
    org_ids: tuple[int, ...] = ()
    roles: dict[int, str] = field(default_factory=dict)
    platform_roles: tuple[str, ...] = ()
    is_minor: bool = False

    @property
    def is_platform_admin(self) -> bool:
        return "platform_admin" in self.platform_roles

    def role_in(self, org_id: int | None) -> str | None:
        return self.roles.get(org_id) if org_id else None


def principal(request: Request, session: Session = Depends(db),
              authorization: str | None = Header(default=None)) -> Principal:
    """Resolve the acting user from the access token.

    Roles and memberships are read on every request rather than trusted from the
    token: a suspension or a role change must take effect immediately, and a
    15-minute token would otherwise carry stale authority for 15 minutes.
    """
    from app.modules.identity.models import OrgMembership, PlatformRoleGrant, User

    if not authorization or not authorization.lower().startswith("bearer "):
        raise Forbidden("Authentication required.", code="unauthenticated")
    token = authorization.split(" ", 1)[1]
    try:
        claims = jwt.decode(token, settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise Forbidden("Invalid or expired token.", code="invalid_token") from None

    user = session.scalars(select(User).where(User.xid == uuid.UUID(claims["sub"]))).first()
    if user is None or user.status != "active":
        raise Forbidden("This account is not active.", code="account_inactive")

    memberships = session.scalars(
        select(OrgMembership).where(OrgMembership.user_id == user.id,
                                    OrgMembership.status == "active")).all()
    platform_roles = session.scalars(
        select(PlatformRoleGrant.role).where(PlatformRoleGrant.user_id == user.id,
                                             PlatformRoleGrant.revoked_at.is_(None))).all()

    resolved = Principal(
        user_id=user.id, user_xid=str(user.xid),
        org_ids=tuple(m.org_id for m in memberships),
        roles={m.org_id: m.role for m in memberships},
        platform_roles=tuple(platform_roles),
        is_minor=user.adult_at > datetime.now(UTC).date(),
    )
    request.state.principal = resolved
    return resolved


def issue_access_token(user_xid: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": user_xid, "iat": now,
         "exp": now + timedelta(seconds=settings().access_token_ttl_seconds)},
        settings().jwt_secret, algorithm="HS256")


class _EntitlementStore(EntitlementStore):
    def __init__(self, session: Session) -> None:
        self._s = session

    def _rows(self, kind: str, ids: list[int], feature: str):
        from app.modules.billing.entitlements import Entitlement
        from app.modules.billing.models import EntitlementRow

        for row in self._s.scalars(
            select(EntitlementRow).where(EntitlementRow.subject_kind == kind,
                                         EntitlementRow.subject_id.in_(ids or [0]),
                                         EntitlementRow.feature == feature)
        ):
            yield Entitlement(
                xid=str(row.id), subject_kind=row.subject_kind,
                subject_xid=str(row.subject_id), feature=row.feature,
                source_kind=row.source_kind, starts_at=row.starts_at,
                expires_at=row.expires_at, revoked_at=row.revoked_at,
                quantity=row.quantity, consumed=row.consumed)

    def for_user(self, user_xid, feature):
        return list(self._rows("user", [int(user_xid)], feature))

    def for_orgs(self, org_xids, feature):
        return list(self._rows("org", [int(o) for o in org_xids], feature))

    def seats_for(self, user_xid):
        from app.modules.billing.entitlements import Seat
        from app.modules.billing.models import SeatAssignment

        return [Seat(entitlement_xid=str(s.entitlement_id), user_xid=str(s.user_id),
                     released_at=s.released_at)
                for s in self._s.scalars(
                    select(SeatAssignment).where(SeatAssignment.user_id == int(user_xid)))]

    def consume(self, entitlement_xid, amount):
        from app.modules.billing.models import EntitlementRow

        row = self._s.get(EntitlementRow, int(entitlement_xid))
        row.consumed += amount
        self._s.flush()


def entitlements(session: Session = Depends(db)) -> Entitlements:
    return Entitlements(_EntitlementStore(session), SystemClock())


def exam_session(session: Session = Depends(db)) -> ExamSession:
    return ExamSession(session, scorer(), SystemClock(),
                       grace_seconds=settings().submit_grace_seconds)


# ── idempotency ──────────────────────────────────────────────────────

@dataclass(slots=True)
class Idempotency:
    """Store-and-replay for the writes a flaky mobile client will retry.

    A replay with the same key and the same body returns the stored response. A
    replay with the same key and a DIFFERENT body is a client bug and gets 409 —
    silently applying it would make the second request invisible.
    """

    session: Session
    key: str | None
    user_id: int
    scope: str = ""

    def replay(self, scope: str, body: Any) -> dict | None:
        if not self.key:
            return None
        self.scope = scope
        digest = _hash(body)
        row = self.session.scalars(
            select(IdempotencyKey).where(IdempotencyKey.scope == scope,
                                         IdempotencyKey.key == self.key)).first()
        if row is None:
            return None
        if row.request_hash != digest:
            raise Conflict("This idempotency key was used with a different request body.",
                           code="idempotency_key_reused")
        return row.response_body

    def store(self, body: Any, response: dict, status: int = 200) -> None:
        if not self.key:
            return
        self.session.add(IdempotencyKey(
            scope=self.scope, key=self.key, user_id=self.user_id,
            request_hash=_hash(body), response_status=status,
            # jsonable_encoder, not `json.dumps(default=str)`: the latter renders
            # a datetime as "2026-07-29 13:14:52+00:00" while FastAPI renders it
            # ISO-8601, so a replay would return a differently-shaped response
            # than the original — defeating the point of storing it.
            response_body=jsonable_encoder(response),
            expires_at=datetime.now(UTC) + timedelta(days=1)))
        self.session.flush()


def _hash(body: Any) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def idempotency(session: Session = Depends(db),
                actor: Principal = Depends(principal),
                idempotency_key: str | None = Header(default=None,
                                                     alias="Idempotency-Key")
                ) -> Idempotency:
    return Idempotency(session=session, key=idempotency_key, user_id=actor.user_id)


def require_not_found(value: Any, what: str = "Resource") -> Any:
    if value is None:
        raise NotFound(f"{what} not found.")
    return value
