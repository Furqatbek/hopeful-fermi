"""Entitlements — the single place the product asks "is this allowed to happen".

Every gated action in the system calls `Entitlements.check()`. Nothing reads
`orders` or `payments` directly, and there is no second implementation of "has
this student paid" hidden in feature code. That centralisation is the whole
requirement (spec §7), and it is worth more than any individual rule below.

Resolution order, most specific first:

    1. A live entitlement held by the USER directly (subscription, one-off, promo).
    2. A seat the user holds against an ORGANIZATION's licence.
    3. An entitlement held by an organization the user belongs to, with no seats.

Consumables are decremented; unlimited grants are not. A revoked grant is dead
immediately regardless of its expiry, because revocation is what a refund and a
ban both need.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Iterable, Protocol

from app.platform.clock import Clock
from app.platform.errors import PaymentRequired


class Reason(StrEnum):
    GRANTED = "granted"
    NO_ENTITLEMENT = "no_entitlement"
    EXPIRED = "expired"
    NOT_STARTED = "not_started"
    REVOKED = "revoked"
    EXHAUSTED = "exhausted"
    NO_SEAT = "no_seat"


@dataclass(frozen=True, slots=True)
class Entitlement:
    xid: str
    subject_kind: str            # 'user' | 'org'
    subject_xid: str
    feature: str
    source_kind: str             # 'order' | 'manual_grant' | 'trial' | 'seat' | 'promo'
    starts_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    quantity: int | None = None  # None = unlimited
    consumed: int = 0

    @property
    def remaining(self) -> int | None:
        return None if self.quantity is None else max(0, self.quantity - self.consumed)

    def status_at(self, now: datetime) -> Reason:
        if self.revoked_at is not None and self.revoked_at <= now:
            return Reason.REVOKED
        if self.starts_at > now:
            return Reason.NOT_STARTED
        if self.expires_at is not None and self.expires_at <= now:
            return Reason.EXPIRED
        if self.quantity is not None and self.remaining == 0:
            return Reason.EXHAUSTED
        return Reason.GRANTED


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: Reason
    entitlement: Entitlement | None = None
    remaining: int | None = None

    def raise_if_denied(self, feature: str) -> None:
        if not self.allowed:
            raise PaymentRequired(
                f"No active entitlement for '{feature}'.",
                feature=feature,
                reason=self.reason.value,
            )


@dataclass(frozen=True, slots=True)
class Seat:
    entitlement_xid: str
    user_xid: str
    released_at: datetime | None = None


class EntitlementStore(Protocol):
    """The port. A repository implements it; tests use an in-memory double."""

    def for_user(self, user_xid: str, feature: str) -> Iterable[Entitlement]: ...
    def for_orgs(self, org_xids: Iterable[str], feature: str) -> Iterable[Entitlement]: ...
    def seats_for(self, user_xid: str) -> Iterable[Seat]: ...
    def consume(self, entitlement_xid: str, amount: int) -> None: ...


class Entitlements:
    __slots__ = ("_store", "_clock")

    def __init__(self, store: EntitlementStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def check(self, *, user_xid: str, feature: str,
              org_xids: Iterable[str] = ()) -> Decision:
        now = self._clock.now()
        org_xids = tuple(org_xids)

        # The most informative denial wins, so the client can say "your plan
        # expired" rather than the useless "you have no plan".
        best_denial = Reason.NO_ENTITLEMENT

        for entitlement in self._store.for_user(user_xid, feature):
            status = entitlement.status_at(now)
            if status is Reason.GRANTED:
                return Decision(True, status, entitlement, entitlement.remaining)
            best_denial = _more_informative(best_denial, status)

        if org_xids:
            held_seats = {s.entitlement_xid for s in self._store.seats_for(user_xid)
                          if s.released_at is None}
            for entitlement in self._store.for_orgs(org_xids, feature):
                status = entitlement.status_at(now)
                if status is not Reason.GRANTED:
                    best_denial = _more_informative(best_denial, status)
                    continue
                # A seat-based licence only covers users who actually hold a seat.
                # Without this, buying 10 seats would entitle a 400-student centre.
                if entitlement.source_kind == "seat" and entitlement.xid not in held_seats:
                    best_denial = _more_informative(best_denial, Reason.NO_SEAT)
                    continue
                return Decision(True, status, entitlement, entitlement.remaining)

        return Decision(False, best_denial)

    def consume(self, *, user_xid: str, feature: str,
                org_xids: Iterable[str] = (), amount: int = 1) -> Decision:
        """Check and decrement in one call.

        Deliberately not two calls: a check-then-consume pair invites a caller to
        forget the second half, and every such omission is a consumable the
        student keeps for free.
        """
        decision = self.check(user_xid=user_xid, feature=feature, org_xids=org_xids)
        decision.raise_if_denied(feature)
        entitlement = decision.entitlement
        assert entitlement is not None
        if entitlement.quantity is None:
            return decision                      # unlimited: nothing to spend
        if (entitlement.remaining or 0) < amount:
            return Decision(False, Reason.EXHAUSTED, entitlement, entitlement.remaining)
        self._store.consume(entitlement.xid, amount)
        return Decision(True, Reason.GRANTED, entitlement,
                        (entitlement.remaining or 0) - amount)

    def require(self, *, user_xid: str, feature: str, org_xids: Iterable[str] = ()) -> Decision:
        decision = self.check(user_xid=user_xid, feature=feature, org_xids=org_xids)
        decision.raise_if_denied(feature)
        return decision


_INFORMATIVENESS = (
    Reason.NO_ENTITLEMENT, Reason.NO_SEAT, Reason.NOT_STARTED,
    Reason.EXHAUSTED, Reason.EXPIRED, Reason.REVOKED,
)


def _more_informative(current: Reason, candidate: Reason) -> Reason:
    return max(current, candidate, key=_INFORMATIVENESS.index)
