"""Entitlements.

The requirement is centralisation: one call site for "is this allowed". These
tests therefore cover the resolution rules exhaustively, because every branch
here is one that feature code must never re-implement.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.billing.entitlements import (
    Decision, Entitlement, Entitlements, Reason, Seat,
)
from app.platform.clock import FrozenClock
from app.platform.errors import PaymentRequired

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class FakeStore:
    """In-memory double for the `EntitlementStore` port."""

    def __init__(self, entitlements=(), seats=()):
        self.entitlements = list(entitlements)
        self.seats = list(seats)
        self.consumed: list[tuple[str, int]] = []

    def for_user(self, user_xid, feature):
        return [e for e in self.entitlements
                if e.subject_kind == "user" and e.subject_xid == user_xid
                and e.feature == feature]

    def for_orgs(self, org_xids, feature):
        wanted = set(org_xids)
        return [e for e in self.entitlements
                if e.subject_kind == "org" and e.subject_xid in wanted
                and e.feature == feature]

    def seats_for(self, user_xid):
        return [s for s in self.seats if s.user_xid == user_xid]

    def consume(self, entitlement_xid, amount):
        self.consumed.append((entitlement_xid, amount))
        for i, e in enumerate(self.entitlements):
            if e.xid == entitlement_xid:
                self.entitlements[i] = replace(e, consumed=e.consumed + amount)


def ent(**kw) -> Entitlement:
    base = dict(xid="e-1", subject_kind="user", subject_xid="u-1",
                feature="mock.unlimited", source_kind="order", starts_at=NOW - timedelta(days=1))
    base.update(kw)
    return Entitlement(**base)


def service(entitlements=(), seats=(), now=NOW) -> tuple[Entitlements, FakeStore]:
    store = FakeStore(entitlements, seats)
    return Entitlements(store, FrozenClock(now)), store


class TestGrants:
    def test_no_entitlement_denies(self):
        svc, _ = service()
        d = svc.check(user_xid="u-1", feature="mock.unlimited")
        assert d.allowed is False and d.reason is Reason.NO_ENTITLEMENT

    def test_live_unlimited_grant_allows(self):
        svc, _ = service([ent()])
        d = svc.check(user_xid="u-1", feature="mock.unlimited")
        assert d.allowed is True and d.remaining is None

    def test_entitlement_for_another_feature_does_not_leak(self):
        svc, _ = service([ent(feature="speaking.live_queue")])
        assert svc.check(user_xid="u-1", feature="mock.unlimited").allowed is False

    def test_entitlement_for_another_user_does_not_leak(self):
        svc, _ = service([ent(subject_xid="u-2")])
        assert svc.check(user_xid="u-1", feature="mock.unlimited").allowed is False


class TestTimeBounds:
    def test_expired_grant_denies_and_says_so(self):
        svc, _ = service([ent(expires_at=NOW - timedelta(hours=1))])
        d = svc.check(user_xid="u-1", feature="mock.unlimited")
        assert d.allowed is False and d.reason is Reason.EXPIRED

    def test_grant_expiring_in_the_future_still_allows(self):
        svc, _ = service([ent(expires_at=NOW + timedelta(hours=1))])
        assert svc.check(user_xid="u-1", feature="mock.unlimited").allowed is True

    def test_expiry_is_exclusive_at_the_boundary(self):
        """A subscription ending at noon is not usable at noon."""
        svc, _ = service([ent(expires_at=NOW)])
        assert svc.check(user_xid="u-1", feature="mock.unlimited").reason is Reason.EXPIRED

    def test_future_grant_is_not_yet_usable(self):
        svc, _ = service([ent(starts_at=NOW + timedelta(days=1))])
        d = svc.check(user_xid="u-1", feature="mock.unlimited")
        assert d.allowed is False and d.reason is Reason.NOT_STARTED

    def test_clock_advance_expires_a_grant(self):
        """Uses the injectable clock, not wall time — the reason it is injectable."""
        clock = FrozenClock(NOW)
        store = FakeStore([ent(expires_at=NOW + timedelta(hours=1))])
        svc = Entitlements(store, clock)
        assert svc.check(user_xid="u-1", feature="mock.unlimited").allowed is True
        clock.advance(hours=2)
        assert svc.check(user_xid="u-1", feature="mock.unlimited").allowed is False


class TestRevocation:
    def test_revoked_grant_denies_even_before_expiry(self):
        """Refunds and bans both need this to bite immediately."""
        svc, _ = service([ent(expires_at=NOW + timedelta(days=30),
                              revoked_at=NOW - timedelta(minutes=1))])
        d = svc.check(user_xid="u-1", feature="mock.unlimited")
        assert d.allowed is False and d.reason is Reason.REVOKED

    def test_future_dated_revocation_does_not_bite_yet(self):
        svc, _ = service([ent(revoked_at=NOW + timedelta(days=1))])
        assert svc.check(user_xid="u-1", feature="mock.unlimited").allowed is True


class TestConsumables:
    def test_quantity_is_reported_as_remaining(self):
        svc, _ = service([ent(feature="competition.entry", quantity=4, consumed=1)])
        d = svc.check(user_xid="u-1", feature="competition.entry")
        assert d.allowed is True and d.remaining == 3

    def test_exhausted_consumable_denies(self):
        svc, _ = service([ent(feature="competition.entry", quantity=4, consumed=4)])
        d = svc.check(user_xid="u-1", feature="competition.entry")
        assert d.allowed is False and d.reason is Reason.EXHAUSTED

    def test_consume_decrements(self):
        svc, store = service([ent(feature="competition.entry", quantity=4, consumed=0)])
        d = svc.consume(user_xid="u-1", feature="competition.entry")
        assert d.allowed is True and d.remaining == 3
        assert store.consumed == [("e-1", 1)]

    def test_consume_on_an_unlimited_grant_spends_nothing(self):
        svc, store = service([ent()])
        assert svc.consume(user_xid="u-1", feature="mock.unlimited").allowed is True
        assert store.consumed == []

    def test_consume_raises_when_denied(self):
        svc, _ = service()
        with pytest.raises(PaymentRequired) as exc:
            svc.consume(user_xid="u-1", feature="competition.entry")
        assert exc.value.status == 402
        assert exc.value.extra["feature"] == "competition.entry"

    def test_consuming_more_than_remains_is_refused_without_spending(self):
        svc, store = service([ent(feature="competition.entry", quantity=2, consumed=1)])
        d = svc.consume(user_xid="u-1", feature="competition.entry", amount=2)
        assert d.allowed is False and d.reason is Reason.EXHAUSTED
        assert store.consumed == []


class TestOrganizationAndSeats:
    ORG_SEAT = ent(xid="e-org", subject_kind="org", subject_xid="org-1",
                   source_kind="seat", feature="mock.unlimited")
    ORG_SITE = ent(xid="e-site", subject_kind="org", subject_xid="org-1",
                   source_kind="order", feature="mock.unlimited")

    def test_org_grant_is_invisible_without_membership(self):
        svc, _ = service([self.ORG_SITE])
        assert svc.check(user_xid="u-1", feature="mock.unlimited").allowed is False

    def test_org_wide_grant_covers_any_member(self):
        svc, _ = service([self.ORG_SITE])
        assert svc.check(user_xid="u-1", feature="mock.unlimited",
                         org_xids=["org-1"]).allowed is True

    def test_seat_licence_requires_an_assigned_seat(self):
        """Otherwise buying 10 seats would entitle a 400-student centre."""
        svc, _ = service([self.ORG_SEAT])
        d = svc.check(user_xid="u-1", feature="mock.unlimited", org_xids=["org-1"])
        assert d.allowed is False and d.reason is Reason.NO_SEAT

    def test_seat_licence_covers_a_user_holding_a_seat(self):
        svc, _ = service([self.ORG_SEAT], seats=[Seat("e-org", "u-1")])
        assert svc.check(user_xid="u-1", feature="mock.unlimited",
                         org_xids=["org-1"]).allowed is True

    def test_released_seat_stops_covering_the_user(self):
        svc, _ = service([self.ORG_SEAT],
                         seats=[Seat("e-org", "u-1", released_at=NOW - timedelta(days=1))])
        assert svc.check(user_xid="u-1", feature="mock.unlimited",
                         org_xids=["org-1"]).reason is Reason.NO_SEAT

    def test_a_seat_in_one_org_does_not_cover_another(self):
        other = ent(xid="e-other", subject_kind="org", subject_xid="org-2",
                    source_kind="seat", feature="mock.unlimited")
        svc, _ = service([other], seats=[Seat("e-org", "u-1")])
        assert svc.check(user_xid="u-1", feature="mock.unlimited",
                         org_xids=["org-2"]).reason is Reason.NO_SEAT


class TestResolutionOrder:
    def test_personal_grant_wins_over_organizational(self):
        """A student who paid privately keeps their own plan even inside a centre,
        so leaving the centre does not silently revoke what they bought."""
        personal = ent(xid="e-user")
        org = ent(xid="e-org", subject_kind="org", subject_xid="org-1")
        svc, _ = service([org, personal])
        d = svc.check(user_xid="u-1", feature="mock.unlimited", org_xids=["org-1"])
        assert d.entitlement is not None and d.entitlement.xid == "e-user"

    def test_org_grant_is_used_when_the_personal_one_has_expired(self):
        expired = ent(xid="e-user", expires_at=NOW - timedelta(days=1))
        org = ent(xid="e-org", subject_kind="org", subject_xid="org-1")
        svc, _ = service([expired, org])
        d = svc.check(user_xid="u-1", feature="mock.unlimited", org_xids=["org-1"])
        assert d.allowed is True and d.entitlement.xid == "e-org"

    def test_a_live_grant_wins_over_any_number_of_dead_ones(self):
        svc, _ = service([
            ent(xid="e-1", revoked_at=NOW - timedelta(days=2)),
            ent(xid="e-2", expires_at=NOW - timedelta(days=1)),
            ent(xid="e-3"),
        ])
        d = svc.check(user_xid="u-1", feature="mock.unlimited")
        assert d.allowed is True and d.entitlement.xid == "e-3"


class TestDenialReasons:
    """The client says "your plan expired" rather than "you have no plan". Worth
    testing because the informative reason is easy to lose in a refactor."""

    def test_the_most_informative_denial_is_reported(self):
        svc, _ = service([
            ent(xid="e-1", feature="competition.entry", quantity=1, consumed=1),
            ent(xid="e-2", feature="competition.entry", expires_at=NOW - timedelta(days=1)),
        ])
        assert svc.check(user_xid="u-1",
                         feature="competition.entry").reason is Reason.EXPIRED

    def test_revocation_outranks_expiry(self):
        svc, _ = service([
            ent(xid="e-1", expires_at=NOW - timedelta(days=1)),
            ent(xid="e-2", revoked_at=NOW - timedelta(days=1)),
        ])
        assert svc.check(user_xid="u-1", feature="mock.unlimited").reason is Reason.REVOKED


class TestRequire:
    def test_require_returns_the_decision_when_allowed(self):
        svc, _ = service([ent()])
        assert isinstance(svc.require(user_xid="u-1", feature="mock.unlimited"), Decision)

    def test_require_raises_402_with_the_reason_attached(self):
        svc, _ = service([ent(expires_at=NOW - timedelta(days=1))])
        with pytest.raises(PaymentRequired) as exc:
            svc.require(user_xid="u-1", feature="mock.unlimited")
        assert exc.value.extra["reason"] == "expired"
        assert exc.value.as_problem()["status"] == 402
