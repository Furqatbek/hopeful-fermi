"""Class membership as the console performs it, and what removal must not break.

The console could create a class and invite somebody straight into one, and
nothing else: an existing student could not be added and nobody could be removed.
`cohort_members.left_at` is read in four places — the member count, a student's
own assignment list, and the expansion of a cohort into `assignment_targets` —
and was written by nothing, so leaving a class was designed throughout the query
layer and reachable from nowhere.
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
    assert response.status_code in (expected or (200, 201, 204)), response.text
    return response.json() if response.content else None


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
def klass(client, seed, admin):
    return _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                           headers=admin, json={"name": "Evening IELTS"}), 201)


class TestTheScreensSequence:
    def test_add_an_existing_student_then_remove_them(
            self, client, db, seed, admin, klass):
        aziza = _student(db, seed, "Aziza")

        members = _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members",
                                  headers=admin,
                                  json={"user_xids": [str(aziza.xid)]}))
        assert [m["user"]["given_name"] for m in members] == ["Aziza"]

        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)
        after = _ok(client.get(f"/api/v1/cohorts/{klass['xid']}/members",
                               headers=admin))
        assert after == []

    def test_removal_writes_both_columns(self, client, db, seed, admin, klass):
        """The listing filters on `status`, the assignment expansion filters on
        `left_at`. Writing one would take a student off the roster while still
        setting them work, or the reverse."""
        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)

        row = db.execute(text("""
            SELECT m.status, m.left_at FROM cohort_members m
            JOIN cohorts c ON c.id = m.cohort_id
            WHERE c.xid = CAST(:c AS uuid) AND m.user_id = :u
        """).bindparams(c=klass["xid"], u=aziza.id)).mappings().one()
        assert row["status"] == "left"
        assert row["left_at"] is not None

    def test_the_member_count_the_class_list_shows_follows(
            self, client, db, seed, admin, klass):
        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        listed = _ok(client.get(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                                headers=admin))
        assert [c["member_count"] for c in listed if c["xid"] == klass["xid"]] == [1]

        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)
        listed = _ok(client.get(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                                headers=admin))
        assert [c["member_count"] for c in listed if c["xid"] == klass["xid"]] == [0]

    def test_removing_somebody_who_is_not_in_it_is_a_404(
            self, client, db, seed, admin, klass):
        outsider = _student(db, seed, "Nodir")
        refused = client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{outsider.xid}", headers=admin)
        assert refused.status_code == 404

    def test_someone_outside_the_centre_cannot_be_added(
            self, client, db, seed, admin, klass):
        """A centre could otherwise add anyone's account to its reporting."""
        from app.modules.identity.models import User

        stranger = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}",
                        given_name="Stranger", date_of_birth=dt.date(2000, 1, 1))
        db.add(stranger)
        db.flush()
        refused = client.post(f"/api/v1/cohorts/{klass['xid']}/members",
                              headers=admin, json={"user_xids": [str(stranger.xid)]})
        assert refused.status_code == 409
        assert refused.json()["code"] == "not_an_org_member"

    def test_a_teacher_may_not_change_class_membership(
            self, client, db, seed, klass):
        """`MANAGE_ORG`, not a teaching role: who is in which class decides who
        gets set work and who appears in whose reports."""
        aziza = _student(db, seed, "Aziza")
        refused = client.post(f"/api/v1/cohorts/{klass['xid']}/members",
                              headers=auth(seed["author"].xid),
                              json={"user_xids": [str(aziza.xid)]})
        assert refused.status_code == 403


class TestRemovalDoesNotRewriteWhatWasAlreadySat:
    def test_the_membership_survives_as_a_record_of_having_been_there(
            self, client, db, seed, admin, klass):
        """Soft, and NOT because past assignments depend on it — a first pass
        claimed that and a sabotage proved it false: `assignment_targets` holds
        `user_id`, so a hard delete leaves every sat mock resolving perfectly.

        It is soft because the row is the only record that this student was ever
        in this class. `joined_at` and `left_at` are what
        `/cohorts/{xid}/attendance` reports against, and deleting the row answers
        "was Aziza in the evening group last term?" with silence."""
        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)
        row = db.execute(text("""
            SELECT m.joined_at, m.left_at FROM cohort_members m
            JOIN cohorts c ON c.id = m.cohort_id
            WHERE c.xid = CAST(:c AS uuid) AND m.user_id = :u
        """).bindparams(c=klass["xid"], u=aziza.id)).mappings().first()
        assert row is not None, "a hard delete loses that they were ever here"
        assert row["joined_at"] and row["left_at"]
        assert row["left_at"] >= row["joined_at"]

    def test_a_removed_student_keeps_the_work_they_were_set(
            self, client, db, seed, admin, klass, published):
        """Removing somebody must not disturb what they have already sat."""
        from app.modules.billing.models import EntitlementRow

        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                              feature="org.assignments", source_kind="order",
                              starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
        db.add(EntitlementRow(subject_kind="user", subject_id=aziza.id,
                              feature="mock.unlimited", source_kind="order",
                              starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
        db.flush()

        now = dt.datetime.now(dt.UTC)
        assignment = _ok(client.post("/api/v1/assignments", headers=admin, json={
            "test_version_xid": str(published["test_version"].xid),
            "target_kind": "cohort", "cohort_xid": klass["xid"],
            "opens_at": (now - dt.timedelta(hours=1)).isoformat(),
            "closes_at": (now + dt.timedelta(days=7)).isoformat(),
            "mode": "exam", "allow_review_after": "close", "max_attempts": 1}), 201)

        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)

        # The target row survives, so the invigilation and results screens still
        # show her against the mock she was set.
        progress = _ok(client.get(f"/api/v1/assignments/{assignment['xid']}/progress",
                                  headers=admin))
        assert [s["user"]["given_name"] for s in progress["students"]] == ["Aziza"]

    def test_a_removed_student_is_not_targeted_by_the_NEXT_assignment(
            self, client, db, seed, admin, klass, published):
        """The half that was unreachable: without a way to write `left_at`, a
        student who changed groups kept receiving that class's mocks for ever."""
        from app.modules.billing.models import EntitlementRow

        aziza = _student(db, seed, "Aziza")
        bek = _student(db, seed, "Bek")
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid), str(bek.xid)]}))
        db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                              feature="org.assignments", source_kind="order",
                              starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
        for user in (aziza, bek):
            db.add(EntitlementRow(subject_kind="user", subject_id=user.id,
                                  feature="mock.unlimited", source_kind="order",
                                  starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
        db.flush()

        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)

        now = dt.datetime.now(dt.UTC)
        assignment = _ok(client.post("/api/v1/assignments", headers=admin, json={
            "test_version_xid": str(published["test_version"].xid),
            "target_kind": "cohort", "cohort_xid": klass["xid"],
            "opens_at": (now - dt.timedelta(hours=1)).isoformat(),
            "closes_at": (now + dt.timedelta(days=7)).isoformat(),
            "mode": "exam", "allow_review_after": "close", "max_attempts": 1}), 201)
        progress = _ok(client.get(f"/api/v1/assignments/{assignment['xid']}/progress",
                                  headers=admin))
        assert [s["user"]["given_name"] for s in progress["students"]] == ["Bek"]


class TestReAddingSomebodyWhoLeft:
    def test_a_student_who_left_can_be_put_back(self, client, db, seed, admin, klass):
        """Terms change and people come back.

        `add_cohort_members` skips anybody with an EXISTING row, and it reads
        every row rather than only the active ones — so once removal started
        writing `status = 'left'`, re-adding the same student matched the old row,
        was skipped, and answered 200 with a list they were still absent from.
        Silently: the caller sees a success and an unchanged roster.
        """
        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)

        back = _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members",
                               headers=admin, json={"user_xids": [str(aziza.xid)]}))
        assert [m["user"]["given_name"] for m in back] == ["Aziza"]

    def test_being_put_back_does_not_leave_two_rows(self, client, db, seed,
                                                    admin, klass):
        """One membership per student per class. Two rows would count them twice
        in `member_count` and target them twice in an assignment."""
        aziza = _student(db, seed, "Aziza")
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        rows = db.execute(text("""
            SELECT count(*) FROM cohort_members m JOIN cohorts c ON c.id = m.cohort_id
            WHERE c.xid = CAST(:c AS uuid) AND m.user_id = :u
        """).bindparams(c=klass["xid"], u=aziza.id)).scalar()
        assert rows == 1

    def test_coming_back_starts_a_new_membership_not_the_old_one(
            self, client, db, seed, admin, klass):
        """`joined_at` is when THIS membership began, and it began again.

        `/cohorts/{xid}/attendance` reports against it, so carrying a date from a
        previous term forward would credit a returning student with weeks they
        were not in the room.
        """
        aziza = _student(db, seed, "Aziza")

        def joined():
            return db.execute(text("""
                SELECT m.joined_at FROM cohort_members m
                JOIN cohorts c ON c.id = m.cohort_id
                WHERE c.xid = CAST(:c AS uuid) AND m.user_id = :u
            """).bindparams(c=klass["xid"], u=aziza.id)).scalar()

        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        first = joined()
        db.execute(text("""
            UPDATE cohort_members SET joined_at = joined_at - interval '90 days'
        """))
        db.flush()
        moved = joined()
        assert moved < first

        _ok(client.delete(
            f"/api/v1/cohorts/{klass['xid']}/members/{aziza.xid}", headers=admin), 204)
        _ok(client.post(f"/api/v1/cohorts/{klass['xid']}/members", headers=admin,
                        json={"user_xids": [str(aziza.xid)]}))
        assert joined() > moved, "the old term's date must not carry forward"
        # And the membership is live again, with no lingering leaving date.
        left = db.execute(text("""
            SELECT m.left_at FROM cohort_members m
            JOIN cohorts c ON c.id = m.cohort_id
            WHERE c.xid = CAST(:c AS uuid) AND m.user_id = :u
        """).bindparams(c=klass["xid"], u=aziza.id)).scalar()
        assert left is None
