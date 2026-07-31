"""Assignments and the regrade gate.

The regrade half is the part a school client will judge us on. A bad answer key
is the fastest way to lose one, so the shape is stage → read the impact → apply,
and `apply` refuses while a finished contest's ranking would move. That refusal
is structural rather than a convention someone has to remember, and this file is
where that claim is checked.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.api.deps import issue_access_token
from app.modules.exam.models import Assignment, AssignmentTarget, Outbox, RegradeJob

NOW = dt.datetime.now(dt.UTC)


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def teacher_auth(seed):
    return {"Authorization": f"Bearer {issue_access_token(str(seed['author'].xid))}"}


@pytest.fixture
def student_auth(seed):
    return {"Authorization": f"Bearer {issue_access_token(str(seed['student'].xid))}"}


@pytest.fixture
def cohort(db, seed):
    from app.modules.identity.models import Cohort, CohortMember

    row = Cohort(org_id=seed["org"].id, name="Evening group",
                 created_by=seed["author"].id)
    db.add(row)
    db.flush()
    db.add(CohortMember(cohort_id=row.id, user_id=seed["student"].id))
    db.flush()
    return row


@pytest.fixture
def org_entitled(db, seed):
    """The centre's contract-level capability.

    `manual_grant`, not `seat`: a seat licence only covers users who hold a seat,
    which is right for a student sitting a mock and wrong for a teacher setting
    one. Setting work is a capability the centre bought, not a seat the teacher
    occupies.
    """
    from app.modules.billing.models import EntitlementRow

    for feature in ("org.assignments", "mock.unlimited"):
        db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                              feature=feature, source_kind="manual_grant",
                              starts_at=NOW - dt.timedelta(days=1)))
    db.flush()
    return seed


def _create(client, headers, published, cohort, **overrides):
    body = {"test_version_xid": str(published["test_version"].xid),
            "target_kind": "cohort", "cohort_xid": str(cohort.xid),
            "opens_at": (NOW - dt.timedelta(hours=1)).isoformat(),
            "closes_at": (NOW + dt.timedelta(days=3)).isoformat(),
            "max_attempts": 1, "mode": "exam", **overrides}
    return client.post("/api/v1/assignments", json=body, headers=headers)


class TestAssignments:
    def test_without_a_seat_entitlement_the_answer_is_402(self, client, teacher_auth,
                                                          published, cohort):
        r = _create(client, teacher_auth, published, cohort)
        assert r.status_code == 402, r.text
        assert r.json()["feature"] == "org.assignments"

    def test_a_draft_version_cannot_be_assigned(self, client, teacher_auth, db, seed,
                                                cohort, org_entitled):
        r = _create(client, teacher_auth, seed, cohort)
        assert r.status_code == 409
        assert r.json()["code"] == "version_not_published"

    def test_targets_are_materialized_at_creation(self, client, teacher_auth, db,
                                                  published, cohort, org_entitled):
        """A cohort's membership changes; the assignment's audience does not.

        Resolving lazily would mean a student who joins next week is silently
        late for work set before they arrived.
        """
        from app.modules.identity.models import CohortMember, User

        r = _create(client, teacher_auth, published, cohort)
        assert r.status_code == 201, r.text
        assignment = db.scalars(
            select(Assignment).where(Assignment.xid == uuid.UUID(r.json()["xid"]))).one()
        assert db.scalar(select(func.count()).select_from(AssignmentTarget)
                         .where(AssignmentTarget.assignment_id == assignment.id)) == 1

        latecomer = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                         given_name="Bekzod",
                         date_of_birth=dt.date(2004, 1, 1))
        db.add(latecomer)
        db.flush()
        db.add(CohortMember(cohort_id=cohort.id, user_id=latecomer.id))
        db.flush()
        assert db.scalar(select(func.count()).select_from(AssignmentTarget)
                         .where(AssignmentTarget.assignment_id == assignment.id)) == 1

    def test_creating_one_emits_an_outbox_event(self, client, teacher_auth, db,
                                                published, cohort, org_entitled):
        """Written in the SAME transaction as the assignment, which is why the
        reminder cannot be lost and why Kafka is not needed."""
        _create(client, teacher_auth, published, cohort)
        event = db.scalars(
            select(Outbox).where(Outbox.event_type == "assignment.created")).one()
        assert event.payload["targets"] == 1
        assert event.dispatched_at is None

    def test_a_closing_date_before_the_opening_date_is_refused(
            self, client, teacher_auth, published, cohort, org_entitled):
        r = _create(client, teacher_auth, published, cohort,
                    closes_at=(NOW - dt.timedelta(days=1)).isoformat())
        assert r.status_code == 409
        assert r.json()["code"] == "invalid_window"

    def test_a_teacher_cannot_assign_to_another_centres_students(
            self, client, teacher_auth, db, published, cohort, org_entitled):
        from app.modules.identity.models import (
            Cohort, CohortMember, Organization, User,
        )

        other = Organization(name="Elsewhere", slug=f"e-{uuid.uuid4().hex[:6]}",
                             status="active")
        outsider = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                        given_name="Sardor", date_of_birth=dt.date(2003, 5, 5))
        db.add_all([other, outsider])
        db.flush()
        their_cohort = Cohort(org_id=other.id, name="Theirs",
                              created_by=outsider.id)
        db.add(their_cohort)
        db.flush()
        db.add(CohortMember(cohort_id=their_cohort.id, user_id=outsider.id))
        db.flush()

        r = _create(client, teacher_auth, published, cohort, target_kind="users",
                    cohort_xid=None, user_xids=[str(outsider.xid)])
        assert r.status_code == 403
        assert r.json()["code"] == "student_not_in_org"

    def test_a_student_sees_the_assignment_they_were_set(
            self, client, teacher_auth, student_auth, published, cohort, org_entitled):
        _create(client, teacher_auth, published, cohort)
        listed = client.get("/api/v1/assignments", headers=student_auth)
        assert listed.status_code == 200, listed.text
        assert [a["test_title"] for a in listed.json()["items"]] == ["Mock 1 v1"]
        assert listed.json()["items"][0]["my_attempts_used"] == 0

    def test_progress_shows_who_has_not_started(self, client, teacher_auth, published,
                                                cohort, org_entitled):
        created = _create(client, teacher_auth, published, cohort).json()
        progress = client.get(f"/api/v1/assignments/{created['xid']}/progress",
                              headers=teacher_auth)
        assert progress.status_code == 200, progress.text
        body = progress.json()
        assert body["summary"] == {"assigned": 1, "not_started": 1,
                                   "in_progress": 0, "submitted": 0}
        assert body["students"][0]["total"] == 3
        assert body["server_now"]

    def test_a_student_cannot_read_the_cohorts_progress(
            self, client, teacher_auth, student_auth, published, cohort, org_entitled):
        created = _create(client, teacher_auth, published, cohort).json()
        r = client.get(f"/api/v1/assignments/{created['xid']}/progress",
                       headers=student_auth)
        assert r.status_code == 404


class TestRegradeGate:
    def _stage(self, client, headers, seed, **overrides):
        body = {"trigger": "answer_key_change", "subject_type": "question_version",
                "subject_xid": str(seed["question_versions"][0].xid),
                "reason": "Key omitted 'bike'", **overrides}
        return client.post("/api/v1/regrades", json=body, headers=headers)

    def test_staging_writes_nothing_to_scores(self, client, teacher_auth, db, seed):
        from app.modules.exam.models import ScoreRun

        before = db.scalar(select(func.count()).select_from(ScoreRun))
        r = self._stage(client, teacher_auth, seed)
        assert r.status_code == 201, r.text
        assert r.json()["dry_run"] is True
        assert r.json()["status"] == "planning"
        assert db.scalar(select(func.count()).select_from(ScoreRun)) == before

    def test_the_planning_pass_is_enqueued_not_run_inline(self, client, teacher_auth,
                                                          db, seed):
        """A popular item can carry ten thousand sat attempts, and a request that
        recomputes ten thousand attempts is a request that times out."""
        self._stage(client, teacher_auth, seed)
        assert db.scalars(
            select(Outbox).where(Outbox.event_type == "regrade.plan_requested")).one()

    def test_include_competitions_is_accepted_and_ignored(self, client, teacher_auth,
                                                          db, seed):
        """A finished contest is never swept into a bulk regrade. Rejecting the
        flag would just move the argument to the client; dropping it makes the
        rule true regardless of what anyone sends."""
        r = self._stage(client, teacher_auth, seed,
                        scope={"include_competitions": True, "from_date": "2026-01-01"})
        job = db.scalars(
            select(RegradeJob).where(RegradeJob.xid == uuid.UUID(r.json()["xid"]))).one()
        assert "include_competitions" not in job.scope
        assert job.scope["from_date"] == "2026-01-01"

    def test_a_planning_job_cannot_be_applied(self, client, teacher_auth, seed):
        staged = self._stage(client, teacher_auth, seed).json()
        r = client.post(f"/api/v1/regrades/{staged['xid']}/apply", headers=teacher_auth)
        assert r.status_code == 409
        assert r.json()["code"] == "regrade_not_ready"

    def test_an_undecided_competition_blocks_the_apply(self, client, teacher_auth,
                                                       db, seed):
        """The refusal that makes "a leaderboard never changes by itself" true.

        Structural, not a convention: the job carries the impact, and `apply`
        cross-checks it against recorded decisions before it will run.
        """
        staged = self._stage(client, teacher_auth, seed).json()
        contest_xid = str(uuid.uuid4())
        db.execute(text("""
            UPDATE regrade_jobs
            SET status = 'ready', attempts_total = 42, scores_changed = 7,
                bands_changed = 3,
                competition_impact = CAST(:impact AS jsonb)
            WHERE xid = :x
        """).bindparams(x=uuid.UUID(staged["xid"]), impact=(
            '[{"competition_xid": "%s", "attempts": 42, "rank_changes": 12, '
            '"podium_changes": 1, "decision_required": true}]' % contest_xid)))
        db.flush()

        r = client.post(f"/api/v1/regrades/{staged['xid']}/apply", headers=teacher_auth)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "competition_decision_required"
        assert r.json()["competitions"] == [contest_xid]

    def test_a_ready_job_with_no_competitions_applies(self, client, teacher_auth,
                                                      db, seed):
        staged = self._stage(client, teacher_auth, seed).json()
        db.execute(text("UPDATE regrade_jobs SET status = 'ready' WHERE xid = :x")
                   .bindparams(x=uuid.UUID(staged["xid"])))
        db.flush()
        r = client.post(f"/api/v1/regrades/{staged['xid']}/apply", headers=teacher_auth)
        assert r.status_code == 202, r.text
        assert r.json()["dry_run"] is False
        assert db.scalars(
            select(Outbox).where(Outbox.event_type == "regrade.apply_requested")).one()

    def test_only_band_changes_are_counted_for_notification(self, client, teacher_auth,
                                                            db, seed):
        """A raw-score wobble that leaves the band alone is not worth a push.

        Notifying on every change trains students to ignore the channel you need
        for the ones that matter.
        """
        staged = self._stage(client, teacher_auth, seed).json()
        db.execute(text("""
            UPDATE regrade_jobs SET scores_changed = 40, bands_changed = 3
            WHERE xid = :x
        """).bindparams(x=uuid.UUID(staged["xid"])))
        db.flush()
        body = client.get(f"/api/v1/regrades/{staged['xid']}",
                          headers=teacher_auth).json()
        assert body["impact"]["scores_changed"] == 40
        assert body["impact"]["students_to_notify"] == 3

    def test_another_teachers_job_is_not_visible(self, client, teacher_auth, db, seed):
        staged = self._stage(client, teacher_auth, seed).json()
        from app.modules.identity.models import OrgMembership, User

        other = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Zilola",
                     date_of_birth=dt.date(1993, 2, 2))
        db.add(other)
        db.flush()
        db.add(OrgMembership(org_id=seed["org"].id, user_id=other.id, role="teacher"))
        db.flush()
        headers = {"Authorization": f"Bearer {issue_access_token(str(other.xid))}"}
        assert client.get(f"/api/v1/regrades/{staged['xid']}",
                          headers=headers).status_code == 404
        assert client.get("/api/v1/regrades", headers=headers).json() == []

    def test_a_student_cannot_stage_a_regrade(self, client, student_auth, seed):
        r = self._stage(client, student_auth, seed)
        assert r.status_code == 403
