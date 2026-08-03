"""Seats as the console manages them, and what releasing one has to change.

`seat_assignments.released_at` was read in three places — the assigned count, the
members list, and `billing.entitlements`, which covers a student only while they
hold an unreleased seat — and was written by nothing. So a seat could be given
and never taken back: a centre on a ten-seat licence was capped at the first ten
students it ever seated, and a student who left went on consuming a seat.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


def _student(db, seed, name):
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name=name,
                date_of_birth=dt.date(2008, 3, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id, role="student",
                         status="active"))
    db.flush()
    return user


@pytest.fixture
def admin(db, seed):
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Rustam",
                date_of_birth=dt.date(1990, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return auth(user.xid)


@pytest.fixture
def two_seats(db, seed):
    """A centre on a TWO-seat licence: small enough that running out is one step."""
    from app.modules.billing.entitlements import SEAT_BUNDLE
    from app.modules.billing.models import EntitlementRow

    row = EntitlementRow(
        subject_kind="org", subject_id=seed["org"].id,
        feature=sorted(SEAT_BUNDLE)[0], source_kind="seat", quantity=2,
        starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
    db.add(row)
    db.flush()
    return row


class TestWhatTheScreenShows:
    def test_no_licence_reads_as_zero_rather_than_an_error(
            self, client, seed, admin):
        """"You have no seats" is the true answer to how many seats you have."""
        summary = _ok(client.get(f"/api/v1/orgs/{seed['org'].xid}/seats",
                                 headers=admin))
        assert summary["entitlement_xid"] is None
        assert (summary["total"], summary["assigned"], summary["remaining"]) == (0, 0, 0)
        assert summary["members"] == []

    def test_the_counts_the_header_renders(self, client, db, seed, admin, two_seats):
        aziza = _student(db, seed, "Aziza")
        summary = _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                                  headers=admin,
                                  json={"user_xids": [str(aziza.xid)]}))
        assert (summary["total"], summary["assigned"], summary["remaining"]) == (2, 1, 1)
        assert [m["given_name"] for m in summary["members"]] == ["Aziza"]

    def test_a_student_cannot_read_who_holds_the_centres_seats(
            self, client, db, seed, two_seats):
        aziza = _student(db, seed, "Aziza")
        refused = client.get(f"/api/v1/orgs/{seed['org'].xid}/seats",
                             headers=auth(aziza.xid))
        assert refused.status_code in (403, 404)


class TestReleasingASeat:
    def test_it_comes_back_to_the_pool(self, client, db, seed, admin, two_seats):
        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        summary = _ok(client.delete(
            f"/api/v1/orgs/{seed['org'].xid}/seats/{aziza.xid}", headers=admin))
        # Returned directly, because the number a centre wants next is how many
        # are now free.
        assert (summary["assigned"], summary["remaining"]) == (0, 2)
        assert summary["members"] == []

    def test_the_history_survives(self, client, db, seed, admin, two_seats):
        """Soft, and here the reason is the money: "we were charged for twelve
        seats" is answered by when each was held and by whom."""
        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        _ok(client.delete(f"/api/v1/orgs/{seed['org'].xid}/seats/{aziza.xid}",
                          headers=admin))
        row = db.execute(text("""
            SELECT assigned_at, released_at, assigned_by FROM seat_assignments
             WHERE user_id = :u
        """).bindparams(u=aziza.id)).mappings().first()
        assert row is not None, "a hard delete answers a billing dispute with nothing"
        assert row["released_at"] is not None and row["assigned_by"]

    def test_releasing_a_seat_nobody_holds_is_a_404(
            self, client, db, seed, admin, two_seats):
        nodir = _student(db, seed, "Nodir")
        refused = client.delete(f"/api/v1/orgs/{seed['org'].xid}/seats/{nodir.xid}",
                                headers=admin)
        assert refused.status_code == 404

    def test_a_teacher_may_not_move_seats(self, client, db, seed, two_seats):
        aziza = _student(db, seed, "Aziza")
        refused = client.delete(f"/api/v1/orgs/{seed['org'].xid}/seats/{aziza.xid}",
                                headers=auth(seed["author"].xid))
        assert refused.status_code == 403


class TestTheSeatCanBeGivenToSomebodyElse:
    def test_a_full_licence_refuses_until_a_seat_is_released(
            self, client, db, seed, admin, two_seats):
        """The whole point of releasing. A centre on two seats with two leavers
        was permanently full."""
        first = _student(db, seed, "Aziza")
        second = _student(db, seed, "Bek")
        third = _student(db, seed, "Dilnoza")
        _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats", headers=admin,
                        json={"user_xids": [str(first.xid), str(second.xid)]}))

        refused = client.post(f"/api/v1/orgs/{seed['org'].xid}/seats", headers=admin,
                              json={"user_xids": [str(third.xid)]})
        assert refused.status_code == 409
        assert refused.json()["code"] == "not_enough_seats"

        _ok(client.delete(f"/api/v1/orgs/{seed['org'].xid}/seats/{first.xid}",
                          headers=admin))
        summary = _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                                  headers=admin,
                                  json={"user_xids": [str(third.xid)]}))
        assert sorted(m["given_name"] for m in summary["members"]) == ["Bek", "Dilnoza"]

    def test_the_same_student_can_be_seated_again(
            self, client, db, seed, admin, two_seats):
        """Assignment matched on user alone and skipped every match, so once a
        seat was released, giving it back answered 200 with a summary they were
        still absent from."""
        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        _ok(client.delete(f"/api/v1/orgs/{seed['org'].xid}/seats/{aziza.xid}",
                          headers=admin))
        summary = _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                                  headers=admin,
                                  json={"user_xids": [str(aziza.xid)]}))
        assert [m["given_name"] for m in summary["members"]] == ["Aziza"]
        rows = db.execute(text(
            "SELECT count(*) FROM seat_assignments WHERE user_id = :u"
        ).bindparams(u=aziza.id)).scalar()
        assert rows == 1, "one row per student per licence; two would double-count"


class TestASeatIsWhatTheAssignmentGateChecks:
    def test_releasing_a_seat_stops_covering_that_student(
            self, client, db, seed, admin, two_seats, published):
        """`billing.entitlements` covers a student only while they hold an
        unreleased seat — this is the gate the Assignments screen's 402 comes
        from, so a released seat has to change its answer."""
        from app.modules.billing.models import EntitlementRow

        aziza = _student(db, seed, "Aziza")
        db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                              feature="org.assignments", source_kind="order",
                              starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
        db.flush()
        cohort = _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                                 headers=admin, json={"name": "Evening"}), 201)
        _ok(client.post(f"/api/v1/cohorts/{cohort['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))

        now = dt.datetime.now(dt.UTC)
        body = {
            "test_version_xid": str(published["test_version"].xid),
            "target_kind": "cohort", "cohort_xid": cohort["xid"],
            "opens_at": (now - dt.timedelta(hours=1)).isoformat(),
            "closes_at": (now + dt.timedelta(days=7)).isoformat(),
            "mode": "exam", "allow_review_after": "close", "max_attempts": 1}
        _ok(client.post("/api/v1/assignments", headers=admin, json=body), 201)

        _ok(client.delete(f"/api/v1/orgs/{seed['org'].xid}/seats/{aziza.xid}",
                          headers=admin))
        refused = client.post("/api/v1/assignments", headers=admin, json=body)
        assert refused.status_code == 402
        # The refusal is WHOLE, never partial: covering some of a class and
        # quietly dropping the rest splits it.
        assert "seat" in refused.text.lower() or "licence" in refused.text.lower()
