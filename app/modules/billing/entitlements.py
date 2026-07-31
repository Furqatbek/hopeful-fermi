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

`SEAT_BUNDLE` is the other half of the same requirement. Centralising the RULES
is worth nothing if two call sites disagree about the KEY — see its docstring for
the pair that did.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Iterable, Protocol

from app.platform.clock import Clock
from app.platform.errors import PaymentRequired


SEAT_BUNDLE: tuple[str, ...] = ("mock.unlimited",)
"""Every feature that licenses ONE STUDENT to sit ONE mock paper.

**A bundle rather than a name because a plan is a row and this was a string.**
`products.features` is jsonb — "adding a plan is a row, not a code change" — and
against that sat one hardcoded `"mock.unlimited"` in a router. A centre could hold
a perfectly good B2B plan, be refused every assignment, and read a 402 naming a
feature nobody had sold them.

It is declared HERE, next to the resolution rules, because two subsystems have to
agree on it and they did not:

  * `teaching._require_covered` asked whether each target is covered, against
    `mock.unlimited`.
  * `billing.assign_seats` and `_seat_summary` found the centre's seat licence by
    `source_kind = 'seat'` **and no feature at all**, taking `.first()`.

So the seat screen and the coverage gate were reading different rows and neither
could tell. `test_billing_and_admin.py` sold three seats for a feature called
`mock_exams` — a string that appears nowhere else in this system — and all ten
seat tests passed. A centre admin would have bought seats, watched them appear as
assigned, and had every assignment refused with `no_seat`, which is precisely the
advice they had just followed.

**What may join this tuple:** a feature that a STUDENT holds or that a seat
carries. Never a centre capability. `org.assignments` in particular must not:
it is org-held with `source_kind='order'|'manual_grant'`, so `check()` grants it
to every member of the org through the org-wide branch — and the gate exists to
stop "buying 10 seats would entitle a 400-student centre". Adding it would make
the check pass for every student at every centre able to set work at all, which
is every centre. That is not a widened gate, it is a deleted one.

The first entry is the one a 402 names as what to buy, so keep it the one you
actually sell.
"""


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

    def check_any(self, *, user_xid: str, features: Iterable[str],
                  org_xids: Iterable[str] = ()) -> Decision:
        """Allowed if ANY feature in the bundle covers this user.

        Here rather than in the caller, for the reason the module exists: a
        `for feature in BUNDLE: check(...)` loop written in a router is a second
        implementation of "has this student paid" the moment someone decides it
        should stop at the first denial, or that a seat licence is close enough.

        The denial is the most informative one across the WHOLE bundle, not the
        last one tried. It is the difference between telling a centre "your
        licence expired" and telling it "you have no licence" — and once a bundle
        has two members, the order they happen to be listed in must not decide
        which sentence a customer reads.
        """
        org_xids = tuple(org_xids)
        best = Decision(False, Reason.NO_ENTITLEMENT)
        for feature in features:
            decision = self.check(user_xid=user_xid, feature=feature,
                                  org_xids=org_xids)
            if decision.allowed:
                return decision
            if _more_informative(best.reason, decision.reason) is not best.reason:
                best = decision
        return best

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

    def require_any(self, *, user_xid: str, features: Iterable[str],
                    org_xids: Iterable[str] = ()) -> Decision:
        """`require`, over a bundle. The 402 names the first member — the one
        that is actually sold — the same choice `teaching` makes."""
        features = tuple(features)
        decision = self.check_any(user_xid=user_xid, features=features,
                                  org_xids=org_xids)
        decision.raise_if_denied(features[0] if features else "")
        return decision


# Total over `Reason`, deliberately. `GRANTED` is not a denial and neither caller
# can reach it — `check` returns before ranking and `check_any` returns on the
# first allowed decision — but a partial ordering means this raises `ValueError`
# on a member of its own argument type, which for a paywall helper is a 500 on
# every assignment in the product. Ranked lowest so a mixed run still reports the
# denial, which is the only answer worth showing.
_INFORMATIVENESS = (
    Reason.GRANTED, Reason.NO_ENTITLEMENT, Reason.NO_SEAT, Reason.NOT_STARTED,
    Reason.EXHAUSTED, Reason.EXPIRED, Reason.REVOKED,
)


def most_informative(reasons: Iterable[Reason]) -> Reason:
    """The denial worth showing, out of several.

    Public because a caller checking a CLASS of students has the same problem
    `check()` has within one student — thirty denials, one sentence — and the
    ranking must not be re-guessed at the call site. An empty run means nothing
    was denied, and `NO_ENTITLEMENT` is the honest floor for that.
    """
    return max(reasons, default=Reason.NO_ENTITLEMENT, key=_INFORMATIVENESS.index)


def _more_informative(current: Reason, candidate: Reason) -> Reason:
    return most_informative((current, candidate))
