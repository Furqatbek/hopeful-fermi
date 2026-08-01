"""The student's side: starting an attempt, the clock, and reading a result.

`test_exam_lifecycle.py` covers the timing authority and play-once through the
self-serve path. This covers the 11% no test executed, which turned out to contain
the entire B2B flow.

**A student could not start an assigned attempt.** `AttemptCreate.assignment_xid`
is accepted by the model, documented in the OpenAPI schema as the primary path
(`test_version_xid` is described there as "Self-serve practice, when no assignment
applies"), supported by `ExamSession.start(assignment_id=...)` — and never read by
the handler. The only reachable branch is:

    if body.test_version_xid is None:
        raise NotFound("A test version or assignment is required.")

So a student opening work their teacher set had to pass `test_version_xid`, which
produces an attempt with `assignment_id = NULL`. That attempt:

  * does not carry the assignment's time limit, mode or review rule;
  * does not count against `max_attempts`;
  * never appears in the teacher's `assignment_progress` view, which joins on it;
  * and charges the STUDENT's `mock.unlimited` entitlement.

That last one inverts the business model. `create_assignment` says it in as many
words — "a school pays per seat and its students never see a paywall for work the
school set" — and the student saw a paywall.

Two smaller ones alongside it: `attempt_xid` is `required` in the `AttemptResult`
schema and was hardcoded `None`, and `read_review` compared
`assignment.closes_at > attempt.submitted_at` without checking whether the attempt
had been submitted, which is a `TypeError` and a 500.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token
from app.modules.billing.entitlements import SEAT_BUNDLE


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


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


def _entitle(db, user_id: int, feature: str = "mock.unlimited") -> None:
    from app.modules.billing.models import EntitlementRow

    db.add(EntitlementRow(subject_kind="user", subject_id=user_id, feature=feature,
                          source_kind="order", starts_at=_now() - dt.timedelta(days=1)))
    db.flush()


@pytest.fixture
def student(db, seed):
    """The seeded student, with their own entitlement for self-serve practice."""
    _entitle(db, seed["student"].id)
    return seed["student"]


def _assignment(db, published, *, targets, opens_in=-3600, closes_in=604800,
                max_attempts=1, mode="exam", allow_review_after="close",
                time_limit_seconds=None):
    from app.modules.exam.models import Assignment, AssignmentTarget

    row = Assignment(
        org_id=published["org"].id, test_version_id=published["test_version"].id,
        assigned_by=published["author"].id, target_kind="users",
        opens_at=_now() + dt.timedelta(seconds=opens_in),
        closes_at=_now() + dt.timedelta(seconds=closes_in),
        max_attempts=max_attempts, mode=mode,
        allow_review_after=allow_review_after,
        time_limit_seconds=time_limit_seconds)
    db.add(row)
    db.flush()
    for user in targets:
        db.add(AssignmentTarget(assignment_id=row.id, user_id=user.id))
    db.flush()
    return row


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


# ── the defect: starting the work your teacher set ───────────────────

class TestStartingAnAssignedAttempt:
    """`assignment_xid` was accepted and never read, so the whole B2B path — the
    half of the product a school pays for — had no way in."""

    def test_an_assignment_xid_starts_an_attempt(self, client, db, seed, published):
        assignment = _assignment(db, published, targets=[seed["student"]])
        response = client.post("/api/v1/attempts",
                               json={"assignment_xid": str(assignment.xid)},
                               headers=auth(seed["student"].xid))
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "in_progress"

    def test_the_attempt_is_linked_to_the_assignment(self, client, db, seed,
                                                     published):
        """Without the link the teacher's progress view — which joins on
        `attempts.assignment_id` — never sees the student sit the paper."""
        assignment = _assignment(db, published, targets=[seed["student"]])
        _ok(client.post("/api/v1/attempts",
                        json={"assignment_xid": str(assignment.xid)},
                        headers=auth(seed["student"].xid)), 201)
        db.expire_all()
        assert db.scalar(text("SELECT assignment_id FROM attempts")) == assignment.id

    def test_it_carries_the_centre_as_org_context(self, client, db, seed, published):
        """"Null means the student's own private practice, which never appears in a
        centre's analytics. Set automatically from the assignment when present." """
        assignment = _assignment(db, published, targets=[seed["student"]])
        _ok(client.post("/api/v1/attempts",
                        json={"assignment_xid": str(assignment.xid)},
                        headers=auth(seed["student"].xid)), 201)
        assert db.scalar(text("SELECT org_context_id FROM attempts")) \
            == published["org"].id

    def test_it_uses_the_assignments_time_limit(self, client, db, seed, published):
        """A teacher setting 20 minutes means 20 minutes. The self-serve path
        falls back to the test's own config, which is a different number."""
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 time_limit_seconds=1200)
        body = _ok(client.post("/api/v1/attempts",
                               json={"assignment_xid": str(assignment.xid)},
                               headers=auth(seed["student"].xid)), 201)
        assert 1190 <= body["seconds_remaining"] <= 1200

    def test_it_uses_the_assignments_mode(self, client, db, seed, published):
        """The assignment decides, not the request. A client asking for `practice`
        against an exam-mode assignment would otherwise get free replay of a paper
        it is about to be marked on."""
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 mode="exam")
        body = _ok(client.post("/api/v1/attempts",
                               json={"assignment_xid": str(assignment.xid),
                                     "mode": "practice"},
                               headers=auth(seed["student"].xid)), 201)
        assert body["mode"] == "exam"

    def test_the_student_needs_no_entitlement_of_their_own(self, client, db, seed,
                                                           published):
        """"A school pays per seat and its students never see a paywall for work
        the school set." The seeded student holds nothing; the centre set the work.
        """
        assignment = _assignment(db, published, targets=[seed["student"]])
        assert not db.scalar(text("SELECT count(*) FROM entitlements"))
        assert client.post("/api/v1/attempts",
                           json={"assignment_xid": str(assignment.xid)},
                           headers=auth(seed["student"].xid)).status_code == 201

    def test_self_serve_still_needs_one(self, client, db, seed, published):
        """The B2C half is unchanged: private practice is the student's own
        purchase."""
        refused = client.post("/api/v1/attempts",
                              json={"test_version_xid":
                                    str(published["test_version"].xid)},
                              headers=auth(seed["student"].xid))
        assert refused.status_code == 402, refused.text

    def test_someone_not_targeted_cannot_start_it(self, client, db, seed, published):
        """A 404, like every other "not yours" in this router: confirming the
        assignment exists tells a prober something they should not learn."""
        outsider = db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998915559001', 'Sardor', CAST('2000-01-01' AS date), 'active')
            RETURNING id, xid
        """)).mappings().one()
        db.flush()
        assignment = _assignment(db, published, targets=[seed["student"]])
        assert client.post("/api/v1/attempts",
                           json={"assignment_xid": str(assignment.xid)},
                           headers=auth(outsider["xid"])).status_code == 404

    def test_a_cohort_member_can_start_it(self, client, db, seed, published):
        """Targets are materialized at creation, but a cohort assignment set
        before a student's row landed must still open for them."""
        from app.modules.exam.models import Assignment
        from app.modules.identity.models import Cohort, CohortMember

        cohort = Cohort(org_id=published["org"].id, name="Evening",
                        created_by=published["author"].id)
        db.add(cohort)
        db.flush()
        db.add(CohortMember(cohort_id=cohort.id, user_id=seed["student"].id))
        assignment = Assignment(
            org_id=published["org"].id, cohort_id=cohort.id,
            test_version_id=published["test_version"].id,
            assigned_by=published["author"].id, target_kind="cohort",
            opens_at=_now() - dt.timedelta(hours=1),
            closes_at=_now() + dt.timedelta(days=7))
        db.add(assignment)
        db.flush()
        assert client.post("/api/v1/attempts",
                           json={"assignment_xid": str(assignment.xid)},
                           headers=auth(seed["student"].xid)).status_code == 201

    def test_an_unknown_assignment_is_a_404(self, client, seed, published):
        assert client.post("/api/v1/attempts",
                           json={"assignment_xid": str(uuid.uuid4())},
                           headers=auth(seed["student"].xid)).status_code == 404

    def test_neither_id_is_a_404(self, client, seed):
        assert client.post("/api/v1/attempts", json={},
                           headers=auth(seed["student"].xid)).status_code == 404


class TestTheAssignmentWindow:
    """The server is the sole authority on time. An assignment's window is a
    deadline like any other."""

    def test_before_it_opens(self, client, db, seed, published):
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 opens_in=3600)
        refused = client.post("/api/v1/attempts",
                              json={"assignment_xid": str(assignment.xid)},
                              headers=auth(seed["student"].xid))
        assert refused.status_code == 425
        assert refused.json()["code"] == "assignment_not_open"
        # The client renders its countdown from these, never from the device clock.
        assert refused.json()["opens_at"] and refused.json()["server_now"]

    def test_after_it_closes(self, client, db, seed, published):
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 opens_in=-7200, closes_in=-3600)
        refused = client.post("/api/v1/attempts",
                              json={"assignment_xid": str(assignment.xid)},
                              headers=auth(seed["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "assignment_closed"

    def test_inside_the_window(self, client, db, seed, published):
        assignment = _assignment(db, published, targets=[seed["student"]])
        assert client.post("/api/v1/attempts",
                           json={"assignment_xid": str(assignment.xid)},
                           headers=auth(seed["student"].xid)).status_code == 201


class TestMaxAttempts:
    """Stored on every assignment since the first migration and enforced nowhere,
    because nothing could create an attempt against an assignment to enforce it
    on. "One attempt" is the default, and it is what a mock exam means."""

    def _sit(self, client, db, seed, assignment):
        body = _ok(client.post("/api/v1/attempts",
                               json={"assignment_xid": str(assignment.xid)},
                               headers=auth(seed["student"].xid)), 201)
        db.execute(text("UPDATE attempts SET status = 'submitted', "
                        "submitted_at = now() WHERE xid = CAST(:x AS uuid)")
                   .bindparams(x=body["xid"]))
        db.flush()
        return body

    def test_a_second_attempt_is_refused_when_only_one_is_allowed(
            self, client, db, seed, published):
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 max_attempts=1)
        self._sit(client, db, seed, assignment)
        refused = client.post("/api/v1/attempts",
                              json={"assignment_xid": str(assignment.xid)},
                              headers=auth(seed["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "attempt_limit_reached"

    def test_a_second_attempt_is_allowed_when_two_are(self, client, db, seed,
                                                      published):
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 max_attempts=2)
        first = self._sit(client, db, seed, assignment)
        second = _ok(client.post("/api/v1/attempts",
                                 json={"assignment_xid": str(assignment.xid)},
                                 headers=auth(seed["student"].xid)), 201)
        assert second["xid"] != first["xid"]
        assert second["attempt_no"] == 2

    def test_resuming_an_unfinished_attempt_is_not_a_second_one(
            self, client, db, seed, published):
        """"Idempotent by (user, assignment): a client that retries because the
        response was lost gets the SAME attempt back, never a second one against
        its attempt limit." """
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 max_attempts=1)
        first = _ok(client.post("/api/v1/attempts",
                                json={"assignment_xid": str(assignment.xid)},
                                headers=auth(seed["student"].xid)), 201)
        again = _ok(client.post("/api/v1/attempts",
                                json={"assignment_xid": str(assignment.xid)},
                                headers=auth(seed["student"].xid)), 201)
        assert first["xid"] == again["xid"]
        assert db.scalar(text("SELECT count(*) FROM attempts")) == 1


# ── the result ───────────────────────────────────────────────────────

class TestTheResult:
    @pytest.fixture
    def scored(self, db, seed, published, student):
        attempt = db.execute(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status,
                                  started_at, submitted_at)
            VALUES (:u, :tv, 'exam', 'scored', now(), now()) RETURNING id, xid
        """).bindparams(u=seed["student"].id,
                        tv=published["test_version"].id)).mappings().one()
        db.execute(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, band, is_current)
            VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 30, 40, 7.0, true)
        """).bindparams(a=attempt["id"]))
        db.flush()
        return attempt

    def test_the_result_names_the_attempt_it_belongs_to(self, client, seed, scored):
        """`attempt_xid` is `required` in the AttemptResult schema and was
        hardcoded `None`, so every result this API has ever returned was invalid
        against its own contract — and a client holding two results could not tell
        which paper either belonged to."""
        body = _ok(client.get(f"/api/v1/attempts/{scored['xid']}/result",
                              headers=auth(seed["student"].xid)))
        assert body["attempt_xid"] == str(scored["xid"])

    def test_the_score_and_band(self, client, seed, scored):
        body = _ok(client.get(f"/api/v1/attempts/{scored['xid']}/result",
                              headers=auth(seed["student"].xid)))
        assert body["raw_score"] == 30.0
        assert body["max_raw"] == 40.0
        assert body["band"] == 7.0
        assert body["status"] == "scored"

    def test_an_unscored_attempt_has_no_result(self, client, db, seed, published,
                                               student):
        attempt_xid = db.scalar(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status, started_at)
            VALUES (:u, :tv, 'exam', 'in_progress', now()) RETURNING xid
        """).bindparams(u=seed["student"].id, tv=published["test_version"].id))
        db.flush()
        assert client.get(f"/api/v1/attempts/{attempt_xid}/result",
                          headers=auth(seed["student"].xid)).status_code == 404

    def test_someone_elses_attempt_is_a_404(self, client, db, seed, scored):
        outsider = db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998915559002', 'Sardor', CAST('2000-01-01' AS date), 'active')
            RETURNING xid
        """)).mappings().one()
        db.flush()
        assert client.get(f"/api/v1/attempts/{scored['xid']}/result",
                          headers=auth(outsider["xid"])).status_code == 404

    def test_an_unknown_attempt_is_a_404(self, client, seed, student):
        assert client.get(f"/api/v1/attempts/{uuid.uuid4()}/result",
                          headers=auth(seed["student"].xid)).status_code == 404


# ── review ───────────────────────────────────────────────────────────

class TestReviewGating:
    """"Gated by the assignment's `allow_review_after`; a self-serve practice
    attempt is always reviewable, because there is nobody to keep it from." """

    def _attempt(self, db, seed, published, assignment, *, submitted=True):
        attempt = db.execute(text("""
            INSERT INTO attempts (user_id, test_version_id, assignment_id, mode,
                                  status, started_at, submitted_at)
            VALUES (:u, :tv, :a, 'exam', :st, now(), CASE WHEN :sub THEN now() END)
            RETURNING id, xid
        """).bindparams(u=seed["student"].id, tv=published["test_version"].id,
                        a=assignment.id if assignment else None,
                        st="scored" if submitted else "in_progress",
                        sub=submitted)).mappings().one()
        if submitted:
            db.execute(text("""
                INSERT INTO score_runs (attempt_id, reason, engine_version,
                                        key_versions, raw_score, max_raw, band,
                                        is_current)
                VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 30, 40, 7.0, true)
            """).bindparams(a=attempt["id"]))
        db.flush()
        return attempt

    def test_never_is_refused(self, client, db, seed, published):
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 allow_review_after="never")
        attempt = self._attempt(db, seed, published, assignment)
        refused = client.get(f"/api/v1/attempts/{attempt['xid']}/review",
                             headers=auth(seed["student"].xid))
        assert refused.status_code == 403
        assert refused.json()["code"] == "review_not_permitted"

    def test_close_is_refused_while_the_assignment_is_open(self, client, db, seed,
                                                           published):
        """The point of `close`: a student who finishes early must not be able to
        read the marking scheme back to the class still sitting it."""
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 allow_review_after="close")
        attempt = self._attempt(db, seed, published, assignment)
        refused = client.get(f"/api/v1/attempts/{attempt['xid']}/review",
                             headers=auth(seed["student"].xid))
        assert refused.status_code == 403
        assert refused.json()["code"] == "review_not_yet_open"

    def test_close_opens_once_the_assignment_has_closed(self, client, db, seed,
                                                        published):
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 opens_in=-7200, closes_in=-3600,
                                 allow_review_after="close")
        attempt = self._attempt(db, seed, published, assignment)
        assert client.get(f"/api/v1/attempts/{attempt['xid']}/review",
                          headers=auth(seed["student"].xid)).status_code == 200

    def test_submit_opens_immediately(self, client, db, seed, published):
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 allow_review_after="submit")
        attempt = self._attempt(db, seed, published, assignment)
        assert client.get(f"/api/v1/attempts/{attempt['xid']}/review",
                          headers=auth(seed["student"].xid)).status_code == 200

    def test_a_self_serve_attempt_is_always_reviewable(self, client, db, seed,
                                                       published, student):
        attempt = self._attempt(db, seed, published, None)
        body = _ok(client.get(f"/api/v1/attempts/{attempt['xid']}/review",
                              headers=auth(seed["student"].xid)))
        assert body["attempt_xid"] == str(attempt["xid"])

    def test_an_unsubmitted_attempt_does_not_500(self, client, db, seed, published):
        """`assignment.closes_at > attempt.submitted_at` with `submitted_at` NULL
        is `datetime > None` — a TypeError, and a 500 to a student who tapped
        Review before submitting.
        """
        assignment = _assignment(db, published, targets=[seed["student"]],
                                 allow_review_after="close")
        attempt = self._attempt(db, seed, published, assignment, submitted=False)
        response = client.get(f"/api/v1/attempts/{attempt['xid']}/review",
                              headers=auth(seed["student"].xid))
        assert response.status_code != 500, response.text
        assert response.status_code == 403
        assert response.json()["code"] == "review_not_yet_open"

    def test_an_unsubmitted_self_serve_attempt_says_it_is_not_scored(
            self, client, db, seed, published, student):
        attempt = self._attempt(db, seed, published, None, submitted=False)
        refused = client.get(f"/api/v1/attempts/{attempt['xid']}/review",
                             headers=auth(seed["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "not_scored"


# ── the rest of the surface ──────────────────────────────────────────

class TestTheAttemptSurface:
    @pytest.fixture
    def live(self, client, db, seed, published, student):
        return _ok(client.post("/api/v1/attempts",
                               json={"test_version_xid":
                                     str(published["test_version"].xid)},
                               headers=auth(seed["student"].xid)), 201)

    def test_an_unknown_test_version_is_a_404(self, client, seed, student):
        assert client.post("/api/v1/attempts",
                           json={"test_version_xid": str(uuid.uuid4())},
                           headers=auth(seed["student"].xid)).status_code == 404

    def test_reading_an_attempt_syncs_the_clock(self, client, seed, live):
        body = _ok(client.get(f"/api/v1/attempts/{live['xid']}",
                              headers=auth(seed["student"].xid)))
        assert body["server_now"]
        assert body["seconds_remaining"] > 0

    def test_the_payload_carries_an_etag(self, client, seed, live):
        response = client.get(f"/api/v1/attempts/{live['xid']}/payload",
                              headers=auth(seed["student"].xid))
        assert response.status_code == 200, response.text
        assert response.headers.get("ETag")

    def test_entering_a_section(self, client, seed, live):
        body = _ok(client.post(f"/api/v1/attempts/{live['xid']}/sections/1/enter",
                               headers=auth(seed["student"].xid)))
        assert body["position"] == 1
        assert body["entered_at"]
        assert body["audio_locked"] is False

    def test_entering_a_section_that_is_not_in_the_paper(self, client, seed, live):
        assert client.post(f"/api/v1/attempts/{live['xid']}/sections/9/enter",
                           headers=auth(seed["student"].xid)).status_code == 404

    def test_submitting_returns_a_result_naming_the_attempt(self, client, seed,
                                                            live):
        body = _ok(client.post(f"/api/v1/attempts/{live['xid']}/submit",
                               headers=auth(seed["student"].xid)))
        assert body["attempt_xid"] == live["xid"]
        assert body["status"] == "scored"

    def test_submitting_twice_returns_the_stored_response(self, client, seed, live):
        headers = {**auth(seed["student"].xid), "Idempotency-Key": "submit-once"}
        first = _ok(client.post(f"/api/v1/attempts/{live['xid']}/submit",
                                headers=headers))
        again = _ok(client.post(f"/api/v1/attempts/{live['xid']}/submit",
                                headers=headers))
        assert first == again

    def test_saving_answers_returns_the_clock(self, client, db, seed, live):
        qv = db.execute(text("SELECT xid FROM question_versions ORDER BY id LIMIT 1"))\
            .scalar()
        body = _ok(client.post(
            f"/api/v1/attempts/{live['xid']}/answers",
            json={"deltas": [{"question_version_xid": str(qv), "slot_key": "s1",
                              "response": {"v": "bike"}, "client_seq": 1}]},
            headers=auth(seed["student"].xid)))
        assert body["accepted"] == 1
        assert body["server_now"] and body["seconds_remaining"] > 0

    def test_a_replayed_answer_batch_is_not_applied_twice(self, client, db, seed,
                                                          live):
        qv = db.execute(text("SELECT xid FROM question_versions ORDER BY id LIMIT 1"))\
            .scalar()
        headers = {**auth(seed["student"].xid), "Idempotency-Key": "batch-1"}
        body = {"deltas": [{"question_version_xid": str(qv), "slot_key": "s1",
                            "response": {"v": "bike"}, "client_seq": 1}]}
        first = _ok(client.post(f"/api/v1/attempts/{live['xid']}/answers",
                                json=body, headers=headers))
        again = _ok(client.post(f"/api/v1/attempts/{live['xid']}/answers",
                                json=body, headers=headers))
        assert first == again

    def test_another_students_attempt_is_a_404_everywhere(self, client, db, seed,
                                                          live):
        outsider = db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998915559003', 'Sardor', CAST('2000-01-01' AS date), 'active')
            RETURNING xid
        """)).mappings().one()
        db.flush()
        headers = auth(outsider["xid"])
        for path, method in ((f"/api/v1/attempts/{live['xid']}", "get"),
                             (f"/api/v1/attempts/{live['xid']}/payload", "get"),
                             (f"/api/v1/attempts/{live['xid']}/submit", "post"),
                             (f"/api/v1/attempts/{live['xid']}/sections/1/enter",
                              "post")):
            assert getattr(client, method)(path, headers=headers).status_code == 404, \
                path


def _make_platform_global(db, seed) -> None:
    """Through the ORM, not a raw UPDATE.

    The request runs on this same session, so a `text("UPDATE tests ...")` leaves
    the already-loaded `Test` in the identity map with its old `visibility` and
    the policy reads the stale value — a test that fails for a reason that has
    nothing to do with the code under it.
    """
    seed["test"].visibility = "platform_global"
    db.flush()


class TestWhatSelfServeMayReach:
    """`POST /attempts` with `test_version_xid` asks two questions: may you read
    this paper, and have you paid for it. It asked neither properly.

    The paid half was a literal `"mock.unlimited"` beside a `SEAT_BUNDLE` the
    assigned path next door already used, and both were skipped outright when the
    client sent `mode="preview"`.

    The read half was not asked at all. `create_assignment` runs it — "a centre
    cannot assign a competitor's test it merely stumbled upon" — and this route
    let a student SIT the same paper. Content defaults to `org_private`; the brief
    calls that a contractual promise. An opaque xid is not an authorization check,
    and every id in this system is in some client's memory.
    """

    @pytest.fixture
    def outsider(self, db):
        """No entitlement, no organization, no relationship to the centre."""
        from app.modules.identity.models import User

        user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                    given_name="Outsider", family_name="Nobody",
                    date_of_birth=dt.date(2000, 1, 1))
        db.add(user)
        db.flush()
        return user

    def _start(self, client, user, published, **body):
        return client.post("/api/v1/attempts", headers=auth(user.xid),
                           json={"test_version_xid":
                                 str(published["test_version"].xid), **body})

    def test_preview_mode_is_refused_before_any_handler_runs(self, client, outsider,
                                                             published):
        """Free mocks for ever, in one word of JSON: the entitlement check read
        `if body.mode != "preview"` and `mode` came from the client."""
        assert self._start(client, outsider, published,
                           mode="preview").status_code == 422

    def test_including_from_a_student_who_has_paid(self, client, db, seed,
                                                   published, student):
        """Not a paywall workaround only — `preview` also skipped the published
        check, so it is refused for everyone rather than for the unpaid."""
        assert self._start(client, student, published,
                           mode="preview").status_code == 422

    def test_a_draft_is_unreachable_now_that_preview_is(self, client, db, seed,
                                                        outsider):
        """`ExamSession.start` refuses an unpublished version — unless the mode is
        `preview`. That was the same word, so the same request reached a draft
        nobody had approved, let alone published."""
        assert db.scalar(text("SELECT status FROM test_versions WHERE id = :v")
                         .bindparams(v=seed["test_version"].id)) == "draft"
        started = client.post(
            "/api/v1/attempts", headers=auth(outsider.xid),
            json={"test_version_xid": str(seed["test_version"].xid),
                  "mode": "preview"})
        assert started.status_code == 422, started.text

    def test_a_rival_centres_paper_is_a_403_even_with_a_valid_plan(
            self, client, db, seed, published, outsider):
        """The leak the paywall was hiding. This outsider HAS paid — they are
        simply not at this centre, and the paper is `org_private`."""
        _entitle(db, outsider.id)
        assert db.scalar(text("SELECT visibility FROM tests WHERE id = :t")
                         .bindparams(t=seed["test"].id)) == "org_private"
        refused = self._start(client, outsider, published)
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "read_not_permitted"

    def test_and_nothing_is_created_when_it_refuses(self, client, db, seed,
                                                    published, outsider):
        """A 403 that still issued the attempt would leave `/payload` reachable,
        which is the whole paper."""
        _entitle(db, outsider.id)
        self._start(client, outsider, published)
        db.rollback()
        assert db.scalar(text("SELECT count(*) FROM attempts WHERE user_id = :u")
                         .bindparams(u=outsider.id)) == 0

    def test_the_centres_own_student_is_unaffected(self, client, db, seed,
                                                   published, student):
        """The regression guard. A gate that refuses the people who paid is worse
        than no gate — it gets switched off by the first support ticket."""
        assert self._start(client, student, published).status_code == 201

    def test_platform_content_is_sittable_by_anyone_who_has_paid(
            self, client, db, seed, published, outsider):
        """The B2C product. `platform_global` is what the platform sells direct,
        and the read check must not close it."""
        _entitle(db, outsider.id)
        _make_platform_global(db, seed)
        assert self._start(client, outsider, published).status_code == 201

    def test_reading_is_checked_before_paying(self, client, db, seed, published,
                                              outsider):
        """Order matters for what it discloses. A 402 tells an outsider the paper
        exists and is sittable; only the price is in the way. They should be told
        no about the paper, not quoted for it."""
        refused = self._start(client, outsider, published)
        assert refused.status_code == 403, refused.text

    def test_the_paywall_names_the_bundle_not_a_literal(self, client, db, seed,
                                                        published, outsider):
        """Third call site, same vocabulary. This one said `"mock.unlimited"`
        while the assigned path had moved to `SEAT_BUNDLE`, which is how the seat
        screen and the coverage gate came to disagree one directory over."""
        _make_platform_global(db, seed)
        refused = self._start(client, outsider, published)
        assert refused.status_code == 402, refused.text
        assert refused.json()["feature"] == SEAT_BUNDLE[0]

    @pytest.mark.parametrize("granted", ["mock.pack", "mock.unlimited"])
    def test_any_bundle_member_pays_for_self_serve(self, client, db, seed,
                                                   published, outsider,
                                                   monkeypatch, granted):
        """Both positions, for the reason `test_teaching_endpoints` spells out: a
        bundle test that only ever grants the first member is a test for a
        constant."""
        from app.api.routers import exam as exam_router

        monkeypatch.setattr(exam_router, "SEAT_BUNDLE",
                            ("mock.pack", "mock.unlimited"))
        _make_platform_global(db, seed)
        _entitle(db, outsider.id, feature=granted)
        assert self._start(client, outsider, published).status_code == 201


class TestThePayloadRecordsWhatItShowed:
    """"Reading this records an `item_exposures` row per item" — the contract,
    since it was drafted. Nothing did.

    Exposure was written only when an attempt was SCORED, on the reasoning that
    "an attempt that was issued and abandoned did not expose anything". The
    student read the paper. That is the exposure, and it is the scraper's whole
    method: start an attempt, pull the payload, never submit, repeat.

    The table feeds `burn_score` — the migration calls it *"has this item burned?
    — the author-facing exposure report"* — and an anti-scrape index on
    `(user_id, occurred_at)`. So the one access pattern both exist to catch was
    the one that recorded nothing at all, and an item read a thousand times
    scored as pristine.
    """

    @pytest.fixture
    def live(self, client, db, seed, published, student):
        return _ok(client.post("/api/v1/attempts",
                               json={"test_version_xid":
                                     str(published["test_version"].xid)},
                               headers=auth(seed["student"].xid)), 201)

    def _read(self, client, seed, live, **headers):
        return client.get(f"/api/v1/attempts/{live['xid']}/payload",
                          headers={**auth(seed["student"].xid), **headers})

    def _rows(self, db):
        return db.execute(text(
            "SELECT question_id, question_version_id, test_version_id, attempt_id,"
            "       user_id, org_id, context FROM item_exposures"
        )).mappings().all()

    def test_one_row_per_item_shown(self, client, db, seed, live, published):
        self._read(client, seed, live)
        rows = self._rows(db)
        shown = db.scalar(text(
            "SELECT total_questions FROM test_versions WHERE id = :v"
        ).bindparams(v=seed["test_version"].id))
        assert len(rows) == shown > 0
        assert {r["user_id"] for r in rows} == {seed["student"].id}
        assert {r["test_version_id"] for r in rows} == {seed["test_version"].id}

    def test_the_rows_name_the_items_the_snapshot_named(self, client, db, seed,
                                                        live):
        """Sourced from the snapshot, so they are the items actually served —
        not whatever the composition has drifted to since publish."""
        body = self._read(client, seed, live).json()
        served = {q["question_version_xid"]
                  for s in body["sections"] for g in s["groups"]
                  for q in g["questions"]}
        recorded = {str(x) for x in db.scalars(text(
            "SELECT qv.xid FROM item_exposures e "
            "JOIN question_versions qv ON qv.id = e.question_version_id"))}
        assert recorded == served

    def test_a_self_serve_read_records_a_null_org(self, client, db, seed, live):
        """The one that 500'd. `org_id` is NULL for private practice, and psycopg
        types a bare NULL parameter as `text`, which PostgreSQL refuses against a
        bigint column — a 500 on every private practice run and none at all on
        assigned work, which is the half a centre would have tested."""
        assert self._read(client, seed, live).status_code == 200
        assert {r["org_id"] for r in self._rows(db)} == {None}
        assert {r["context"] for r in self._rows(db)} == {"exam"}

    def test_forty_reads_expose_once(self, client, db, seed, live):
        """A flaky connection is not forty students. `burn_score` counts
        `count(*)` and `count(DISTINCT user_id)`, so a re-read that added rows
        would retire a healthy item."""
        for _ in range(40):
            self._read(client, seed, live)
        assert len(self._rows(db)) == db.scalar(text(
            "SELECT total_questions FROM test_versions WHERE id = :v"
        ).bindparams(v=seed["test_version"].id))

    def test_the_score_time_backfill_then_adds_nothing(self, client, db, seed,
                                                       live):
        """Two writers, one guard. The backfill still exists for attempts that
        predate this and for any path that reaches a score without the payload
        endpoint; it must not double-count the ones that came through here."""
        from app.modules.analytics.projections import record_exposure

        self._read(client, seed, live)
        before = len(self._rows(db))
        attempt_id = db.scalar(text(
            "SELECT id FROM attempts WHERE xid = CAST(:x AS uuid)"
        ).bindparams(x=str(live["xid"])))
        assert record_exposure(db, attempt_id) == 0
        assert len(self._rows(db)) == before

    def test_nothing_is_recorded_when_the_read_is_refused(self, client, db, seed,
                                                          live):
        """A 404 must not expose. Otherwise probing attempt ids would write
        exposure rows for papers nobody was shown."""
        from app.modules.identity.models import User

        stranger = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                        given_name="Stranger", date_of_birth=dt.date(2000, 1, 1))
        db.add(stranger)
        db.flush()
        assert client.get(f"/api/v1/attempts/{live['xid']}/payload",
                          headers=auth(stranger.xid)).status_code == 404
        assert self._rows(db) == []


class TestThePayloadIsConditional:
    """`ETag` was set and `If-None-Match` was never read, so every revalidation
    re-sent the whole paper.

    This is the largest response in the product and its audience is on Uzbek
    mobile data. The client already stores the payload for offline resilience —
    that is what makes a conditional request the normal case here rather than an
    optimisation — and it was paying full price for every one.
    """

    @pytest.fixture
    def live(self, client, db, seed, published, student):
        return _ok(client.post("/api/v1/attempts",
                               json={"test_version_xid":
                                     str(published["test_version"].xid)},
                               headers=auth(seed["student"].xid)), 201)

    def _read(self, client, seed, live, **headers):
        return client.get(f"/api/v1/attempts/{live['xid']}/payload",
                          headers={**auth(seed["student"].xid), **headers})

    def test_a_matching_validator_is_a_304_with_no_body(self, client, seed, live):
        etag = self._read(client, seed, live).headers["ETag"]
        again = self._read(client, seed, live, **{"If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["ETag"] == etag

    def test_a_stale_validator_still_gets_the_paper(self, client, seed, live):
        """The half that matters for correctness: a republished version must not
        be served from a client copy of the old one."""
        stale = self._read(client, seed, live, **{"If-None-Match": '"nope"'})
        assert stale.status_code == 200
        assert stale.json()["sections"]

    def test_a_star_matches_anything_that_exists(self, client, seed, live):
        """RFC 9110. `*` is what a resumed download sends."""
        assert self._read(client, seed, live,
                          **{"If-None-Match": "*"}).status_code == 304

    def test_a_weak_validator_matches(self, client, seed, live):
        """`W/"x"` and `"x"` compare equal for `If-None-Match`. A proxy that
        weakens the tag in transit must not cost the client the whole paper."""
        etag = self._read(client, seed, live).headers["ETag"]
        assert self._read(client, seed, live,
                          **{"If-None-Match": f"W/{etag}"}).status_code == 304

    def test_one_of_several_candidates_matches(self, client, seed, live):
        """It is a list. A client holding two versions sends both, and a bare
        `==` against the header would miss."""
        etag = self._read(client, seed, live).headers["ETag"]
        assert self._read(client, seed, live,
                          **{"If-None-Match": f'"other", {etag}'}).status_code == 304

    def test_an_empty_header_is_not_a_match(self, client, seed, live):
        assert self._read(client, seed, live,
                          **{"If-None-Match": ""}).status_code == 200

    def test_the_paper_is_never_cacheable_by_a_shared_cache(self, client, seed,
                                                            live):
        """`private` so no proxy between Tashkent and this server may hold an
        exam paper; `no-cache` so the client revalidates rather than serving a
        stale one. NOT `no-store`, which would forbid the client copy the offline
        design depends on."""
        for response in (self._read(client, seed, live),
                         self._read(client, seed, live,
                                    **{"If-None-Match": "*"})):
            directives = {d.strip() for d in
                          response.headers["Cache-Control"].split(",")}
            assert {"private", "no-cache"} == directives

    def test_a_draft_preview_has_no_validator_to_match(self, client, db, seed):
        """The author's preview of an UNPUBLISHED version, which is the only way
        to reach a snapshot with no checksum.

        `preview_version` materializes `tv.snapshot` on demand — the publish gate
        has not run, so a broken test is deliberately renderable — and does not
        set `tv.checksum`, because there is nothing to check yet. No ETag is
        exactly right: the draft changes under the author between reads, and a
        stable validator would hand them yesterday's paper and call it current.

        `Cache-Control` is still sent. A draft is the LAST thing a shared cache
        should hold.
        """
        started = client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview",
            headers=auth(seed["author"].xid))
        assert started.status_code == 201, started.text
        assert db.scalar(text("SELECT checksum FROM test_versions WHERE id = :v")
                         .bindparams(v=seed["test_version"].id)) is None

        payload = client.get(f"/api/v1/attempts/{started.json()['xid']}/payload",
                             headers=auth(seed["author"].xid))
        assert payload.status_code == 200, payload.text
        assert payload.json()["sections"]
        assert "ETag" not in payload.headers
        assert payload.headers["Cache-Control"] == "private, no-cache"

    def test_and_a_preview_read_is_recorded_as_preview_exposure(self, client, db,
                                                                seed):
        """`refresh_exposure` filters `context <> 'preview'`, so an author
        checking their own paper must not burn it. The row is still written —
        the anti-scrape index on `(user_id, occurred_at)` wants to see an account
        touching an abnormal number of items whoever they are."""
        started = client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview",
            headers=auth(seed["author"].xid))
        client.get(f"/api/v1/attempts/{started.json()['xid']}/payload",
                   headers=auth(seed["author"].xid))
        contexts = list(db.scalars(text("SELECT DISTINCT context FROM item_exposures")))
        assert contexts == ["preview"]


class TestAVoidedAttemptLosesThePaper:
    """Voiding is an operator action — nothing in the application sets `voided`,
    so it is a platform admin invalidating an attempt: a suspected cheat, a
    duplicate, a session somebody killed.

    Serving the paper afterwards leaves the account that was voided for copying
    with an open door to the thing it was copying. The database already treats
    the state as terminal — `attempt_answers_frozen` refuses writes for
    `submitted`, `scored` and `voided` alike — and the read side did not.

    **Submitted keeps it.** Freezing the ANSWERS at submit and withdrawing the
    QUESTIONS at submit are different rules and only the first is wanted: a
    student reviewing needs the questions in front of the marking, and
    `allow_review_after` governs the answers rather than the paper.
    """

    @pytest.fixture
    def sat(self, client, db, seed, published, student):
        started = _ok(client.post("/api/v1/attempts",
                                  json={"test_version_xid":
                                        str(published["test_version"].xid)},
                                  headers=auth(seed["student"].xid)), 201)
        _ok(client.post(f"/api/v1/attempts/{started['xid']}/submit",
                        headers=auth(seed["student"].xid)))
        return started

    def _void(self, db, xid):
        db.execute(text("UPDATE attempts SET status = 'voided' "
                        "WHERE xid = CAST(:x AS uuid)").bindparams(x=str(xid)))
        db.flush()

    def _payload(self, client, seed, xid):
        return client.get(f"/api/v1/attempts/{xid}/payload",
                          headers=auth(seed["student"].xid))

    def test_a_submitted_attempt_still_serves_it(self, client, db, seed, sat):
        """The explicit half of the decision, pinned so a later tightening of the
        rule above cannot quietly take review with it."""
        response = self._payload(client, seed, sat["xid"])
        assert response.status_code == 200, response.text
        assert response.json()["sections"]

    def test_a_scored_attempt_still_serves_it(self, client, db, seed, sat):
        db.execute(text("UPDATE attempts SET status = 'scored' "
                        "WHERE xid = CAST(:x AS uuid)")
                   .bindparams(x=str(sat["xid"])))
        db.flush()
        assert self._payload(client, seed, sat["xid"]).status_code == 200

    def test_a_voided_attempt_does_not(self, client, db, seed, sat):
        self._void(db, sat["xid"])
        refused = self._payload(client, seed, sat["xid"])
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "attempt_voided"

    def test_it_says_voided_rather_than_hiding_the_attempt(self, client, db, seed,
                                                           sat):
        """409 rather than 404. The attempt is theirs and `GET /attempts/{xid}`
        already shows the status — answering 404 here would tell a student their
        attempt has vanished, which sends them to support instead of to whoever
        voided it."""
        self._void(db, sat["xid"])
        assert client.get(f"/api/v1/attempts/{sat['xid']}",
                          headers=auth(seed["student"].xid)).status_code == 200

    def test_a_voided_attempt_in_progress_loses_it_too(self, client, db, seed,
                                                       published, student):
        """The case voiding actually exists for: an invigilator kills a live
        attempt. The paper must go with it rather than at submit, because there
        will be no submit."""
        started = _ok(client.post("/api/v1/attempts",
                                  json={"test_version_xid":
                                        str(published["test_version"].xid)},
                                  headers=auth(seed["student"].xid)), 201)
        assert self._payload(client, seed, started["xid"]).status_code == 200
        self._void(db, started["xid"])
        assert self._payload(client, seed, started["xid"]).status_code == 409

    def test_the_refusal_records_no_exposure(self, client, db, seed, published,
                                             student):
        """`record_payload_exposure` runs after `exam.payload`, so a refusal
        writes nothing. An exposure row for a paper that was never served would
        burn an item on a request that failed."""
        started = _ok(client.post("/api/v1/attempts",
                                  json={"test_version_xid":
                                        str(published["test_version"].xid)},
                                  headers=auth(seed["student"].xid)), 201)
        self._void(db, started["xid"])
        assert self._payload(client, seed, started["xid"]).status_code == 409
        db.rollback()
        assert db.scalar(text("SELECT count(*) FROM item_exposures")) == 0

    def test_autosave_says_voided_rather_than_submitted(self, client, db, seed,
                                                        published, student):
        """It said "This attempt is submitted; answers are frozen." for a voided
        attempt. The database freezes both the same way, but a student whose
        attempt an operator voided was being told they had submitted it — which
        sends them looking for a result that does not exist."""
        started = _ok(client.post("/api/v1/attempts",
                                  json={"test_version_xid":
                                        str(published["test_version"].xid)},
                                  headers=auth(seed["student"].xid)), 201)
        body = self._payload(client, seed, started["xid"]).json()
        question = body["sections"][0]["groups"][0]["questions"][0]
        self._void(db, started["xid"])
        refused = client.post(
            f"/api/v1/attempts/{started['xid']}/answers",
            headers=auth(seed["student"].xid),
            json={"deltas": [{"question_version_xid":
                              question["question_version_xid"],
                              "slot_key": question["slot_keys"][0],
                              "response": {"text": "x"}, "client_seq": 1}]})
        assert refused.status_code == 409
        assert refused.json()["code"] == "attempt_voided"
        assert "voided" in refused.json()["title"]

    def test_a_submitted_attempt_still_says_frozen(self, client, db, seed, sat):
        """The other branch of the same split, so the new code cannot swallow
        it."""
        refused = client.post(
            f"/api/v1/attempts/{sat['xid']}/answers",
            headers=auth(seed["student"].xid),
            json={"deltas": [{"question_version_xid": str(uuid.uuid4()),
                              "slot_key": "s1", "response": {"text": "x"},
                              "client_seq": 1}]})
        assert refused.status_code == 409
        assert refused.json()["code"] == "attempt_frozen"


def _sit_and_submit(client, db, seed, published, answer="map"):
    h = auth(seed["student"].xid)
    xid = _ok(client.post("/api/v1/attempts", headers=h,
                          json={"test_version_xid":
                                str(published["test_version"].xid)}), 201)["xid"]
    body = _ok(client.get(f"/api/v1/attempts/{xid}/payload", headers=h))
    q = body["sections"][0]["groups"][0]["questions"][0]
    _ok(client.post(f"/api/v1/attempts/{xid}/answers", headers=h,
                    json={"deltas": [{"question_version_xid": q["question_version_xid"],
                                      "slot_key": q["slot_keys"][0],
                                      "response": {"text": answer},
                                      "client_seq": 1}]}))
    _ok(client.post(f"/api/v1/attempts/{xid}/submit", headers=h))
    return xid, body


class TestTheResultIsTheWholeResult:
    """`AttemptResult` declares ten fields. Three were never returned, and each is
    a question a student asks about their own paper."""

    @pytest.fixture
    def sat(self, client, db, seed, published, student):
        return _sit_and_submit(client, db, seed, published)[0]

    def _result(self, client, seed, xid):
        return client.get(f"/api/v1/attempts/{xid}/result",
                          headers=auth(seed["student"].xid))

    def test_it_says_when_it_was_scored(self, client, seed, sat):
        """Without it a result has no age, and a regraded one is
        indistinguishable from the original."""
        assert _ok(self._result(client, seed, sat))["scored_at"]

    def test_it_reports_the_overrun_it_recorded(self, client, db, seed, sat):
        """`attempts.late_by_ms` is written on submit and was read by nothing. Its
        own column comment — "Recorded, not punished: four seconds late on a
        mobile network is a hiccup" — only means something if the student can see
        the four seconds were noticed and cost nothing."""
        db.execute(text("UPDATE attempts SET late_by_ms = 4000 "
                        "WHERE xid = CAST(:x AS uuid)").bindparams(x=str(sat)))
        db.flush()
        assert _ok(self._result(client, seed, sat))["late_by_ms"] == 4000

    def test_a_punctual_attempt_reports_no_overrun(self, client, seed, sat):
        assert _ok(self._result(client, seed, sat))["late_by_ms"] is None

    def test_a_fresh_score_is_not_flagged_as_regraded(self, client, seed, sat):
        assert _ok(self._result(client, seed, sat))["regraded"] is False

    def test_a_regraded_score_is(self, client, db, seed, sat):
        """The student-facing half of the regrade flow. A band that changed by
        itself, with nothing saying anybody changed it, is what makes a school
        stop trusting the platform — and "bad keys are the fastest way to lose a
        school client" is why the regrade pipeline exists at all."""
        db.execute(text("UPDATE score_runs SET reason = 'regrade_key' "
                        "WHERE attempt_id = (SELECT id FROM attempts "
                        "                     WHERE xid = CAST(:x AS uuid))")
                   .bindparams(x=str(sat)))
        db.flush()
        assert _ok(self._result(client, seed, sat))["regraded"] is True

    def test_a_voided_attempt_has_no_result_to_show(self, client, db, seed, sat):
        """Same reasoning as the paper: a band an operator invalidated is a claim
        arising from an invalidated attempt."""
        db.execute(text("UPDATE attempts SET status = 'voided' "
                        "WHERE xid = CAST(:x AS uuid)").bindparams(x=str(sat)))
        db.flush()
        refused = self._result(client, seed, sat)
        assert refused.status_code == 409
        assert refused.json()["code"] == "attempt_voided"


class TestReviewIdentifiesItemsTheWayTheStudentSawThem:
    """`GET /attempts/{xid}/review` is "the single most useful support tool in
    the product" — and it could not say which question any of its rows was about.

    It returned `question_version_id`, an internal sequential bigint, where the
    contract declares `question_version_xid`. Two faults in one field: the
    document's first convention is "internal bigint keys are never exposed —
    sequential ids would turn the content library into a scraping API", and the
    payload the student sat identifies questions by xid, so nothing could join a
    review row to the question it referred to.
    """

    @pytest.fixture
    def sat(self, client, db, seed, published, student):
        return _sit_and_submit(client, db, seed, published)

    def _review(self, client, seed, xid):
        return client.get(f"/api/v1/attempts/{xid}/review",
                          headers=auth(seed["student"].xid))

    def test_no_internal_id_is_returned(self, client, seed, sat):
        body = _ok(self._review(client, seed, sat[0]))
        assert all("question_version_id" not in item for item in body["items"])

    def test_every_item_joins_to_the_paper_the_student_sat(self, client, seed, sat):
        """The functional half. A review row the client cannot match to a
        question is a verdict with no question attached."""
        xid, paper = sat
        served = {q["question_version_xid"]
                  for s in paper["sections"] for g in s["groups"]
                  for q in g["questions"]}
        reviewed = {item["question_version_xid"]
                    for item in _ok(self._review(client, seed, xid))["items"]}
        assert reviewed and reviewed <= served

    def test_items_carry_the_number_the_student_saw(self, client, seed, sat):
        """From the SNAPSHOT, not recomputed: the snapshot is what was served,
        and a composition that has moved on since publish would number a paper
        this student never sat."""
        xid, paper = sat
        first = paper["sections"][0]["groups"][0]["questions"][0]
        item = _ok(self._review(client, seed, xid))["items"][0]
        assert item["number"] == first["number"]

    def test_the_band_is_on_the_review_too(self, client, seed, sat):
        """Declared at the top of `AttemptReview` and never returned. Without it
        the client calls `/result` as well to render its own heading, and the two
        can disagree if a regrade lands between the calls."""
        body = _ok(self._review(client, seed, sat[0]))
        assert "band" in body
        assert body["band"] == _ok(client.get(
            f"/api/v1/attempts/{sat[0]}/result",
            headers=auth(seed["student"].xid)))["band"]

    def test_a_voided_attempt_has_no_review(self, client, db, seed, sat):
        db.execute(text("UPDATE attempts SET status = 'voided' "
                        "WHERE xid = CAST(:x AS uuid)").bindparams(x=str(sat[0])))
        db.flush()
        refused = self._review(client, seed, sat[0])
        assert refused.status_code == 409
        assert refused.json()["code"] == "attempt_voided"


class TestReviewDoesNotLeakALiveContest:
    """This returns `accepted_answers` for every item — the answer key.

    An entrant who finished early could read it while the contest was still
    running and hand it to everyone still sitting. Every other part of the
    competition design exists to prevent exactly that: the payload is AES-GCM
    encrypted in the lobby, the key is a hundred bytes released at T-0, the fetch
    is jittered so the start cannot be timed from traffic. All of it is
    decoration if the answers are one request away the moment somebody submits.
    """

    def _contest(self, db, seed, *, ends_in):
        now = _now()
        return db.execute(text("""
            INSERT INTO competitions (org_id, test_version_id, title, visibility,
                                      status, registration_closes_at, lobby_opens_at,
                                      starts_at, duration_seconds, ends_at,
                                      payload_key_id, created_by)
            VALUES (:o, :tv, 'Winter Open', 'public', 'live', :t0, :t0, :t0, 3600,
                    :t1, 'k1', :u)
            RETURNING id
        """).bindparams(o=seed["org"].id, tv=seed["test_version"].id,
                        u=seed["author"].id, t0=now - dt.timedelta(minutes=5),
                        t1=now + ends_in)).scalar()

    def _entered(self, client, db, seed, published, *, ends_in):
        xid, _ = _sit_and_submit(client, db, seed, published)
        db.execute(text("UPDATE attempts SET competition_id = :c "
                        "WHERE xid = CAST(:x AS uuid)")
                   .bindparams(c=self._contest(db, seed, ends_in=ends_in),
                               x=str(xid)))
        db.flush()
        return xid

    def test_the_answer_key_is_refused_while_the_contest_runs(
            self, client, db, seed, published, student):
        xid = self._entered(client, db, seed, published,
                            ends_in=dt.timedelta(hours=1))
        refused = client.get(f"/api/v1/attempts/{xid}/review",
                             headers=auth(seed["student"].xid))
        assert refused.status_code == 425, refused.text
        assert refused.json()["code"] == "competition_still_live"
        assert "accepted_answers" not in refused.text

    def test_and_it_says_when_review_opens(self, client, db, seed, published,
                                           student):
        """`ends_at` and `server_now`, like every other timed refusal in this
        product — the client renders the countdown from the delta, never from the
        device clock."""
        xid = self._entered(client, db, seed, published,
                            ends_in=dt.timedelta(hours=1))
        body = client.get(f"/api/v1/attempts/{xid}/review",
                          headers=auth(seed["student"].xid)).json()
        assert body["ends_at"] and body["server_now"]

    def test_once_the_contest_has_ended_review_opens(self, client, db, seed,
                                                     published, student):
        """Gated on `ends_at`, not on the leaderboard being written. Making a
        student wait for a ranking job is punishing them for our scheduling."""
        xid = self._entered(client, db, seed, published,
                            ends_in=-dt.timedelta(minutes=1))
        assert client.get(f"/api/v1/attempts/{xid}/review",
                          headers=auth(seed["student"].xid)).status_code == 200

    def test_an_ordinary_attempt_is_unaffected(self, client, db, seed, published,
                                               student):
        """The regression guard. Most attempts have no contest at all."""
        xid, _ = _sit_and_submit(client, db, seed, published)
        assert client.get(f"/api/v1/attempts/{xid}/review",
                          headers=auth(seed["student"].xid)).status_code == 200


class TestListeningReviewReachesTheTranscript:
    """"Optional transcript upload, used for post-exam review, never exposed
    during the exam."

    Post-exam review had no way to reach one. The segments were stored in the
    right shape — migration 0007: "segment form (not a blob) so review can jump
    to the moment a question came from" — the contract declared `audio_range` and
    `transcript_excerpt` on every review item, and nothing joined the two. The
    only route to a transcript was the AUTHORING endpoint, which correctly
    refuses students, so the feature was unimplemented rather than leaky.
    """

    SEGMENTS = [
        {"start_ms": 0, "end_ms": 4000, "speaker": "narrator",
         "text": "You will hear a conversation in a university library."},
        {"start_ms": 4000, "end_ms": 9000, "speaker": "A",
         "text": "I came by bicycle this morning."},
        {"start_ms": 9000, "end_ms": 14000, "speaker": "B",
         "text": "The bus would have been faster."},
    ]

    @pytest.fixture
    def listening(self, db, seed, with_audio):
        """A group whose questions were asked about 5-8 s of the audio.

        Deliberately INSIDE a segment rather than aligned to one. Aligned to the
        4000-9000 boundary, `overlap` and `containment` return the same answer
        and the sabotage that swaps one for the other passes — which is how this
        fixture was written first. Real audio does not stop speaking on the
        boundaries an author draws.
        """
        db.execute(text("""
            INSERT INTO transcripts (audio_track_id, language, body, source, created_by)
            VALUES (:t, 'en', CAST(:b AS jsonb), 'uploaded', :u)
        """).bindparams(t=seed["audio_track"].id, b=json.dumps(self.SEGMENTS),
                        u=seed["author"].id))
        db.execute(text("UPDATE test_version_groups SET audio_start_ms = 5000, "
                        "audio_end_ms = 8000"))
        db.execute(text("UPDATE test_version_sections SET skill = 'listening'"))
        db.flush()
        return seed

    @pytest.fixture
    def sat(self, client, db, seed, listening, student):
        from app.modules.content import repo as content_repo
        from app.platform.clock import SystemClock

        # Re-publish so the snapshot carries the audio range the group now has.
        db.execute(text("UPDATE test_versions SET status = 'draft' WHERE id = :v")
                   .bindparams(v=seed["test_version"].id))
        db.flush()
        content_repo.publish(db, seed["test_version"].id, seed["author"].id,
                             SystemClock().now())
        db.flush()
        return _sit_and_submit(client, db, seed,
                               {"test_version": seed["test_version"]})

    def test_the_item_carries_the_span_it_was_asked_about(self, client, seed, sat):
        item = _ok(client.get(f"/api/v1/attempts/{sat[0]}/review",
                              headers=auth(seed["student"].xid)))["items"][0]
        assert item["audio_range"] == {"start_ms": 5000, "end_ms": 8000}

    def test_a_sentence_straddling_the_boundary_is_included(self, client, seed, sat):
        """Overlap, not containment. The answer is spoken in one sentence that
        began before the author's marker and ends after it — requiring the
        segment to sit wholly inside the span drops exactly the line the student
        is looking for, and drops it silently."""
        item = _ok(client.get(f"/api/v1/attempts/{sat[0]}/review",
                              headers=auth(seed["student"].xid)))["items"][0]
        straddling = self.SEGMENTS[1]
        assert straddling["start_ms"] < 5000 and straddling["end_ms"] > 8000
        assert straddling["text"] in item["transcript_excerpt"]

    def test_and_the_words_that_were_spoken_in_it(self, client, seed, sat):
        item = _ok(client.get(f"/api/v1/attempts/{sat[0]}/review",
                              headers=auth(seed["student"].xid)))["items"][0]
        assert "bicycle" in item["transcript_excerpt"]

    def test_the_excerpt_is_cut_to_the_span(self, client, seed, sat):
        """Not the whole transcript. Handing back every word makes the feature a
        transcript download with extra steps, which is the thing the authoring
        endpoint refuses students for."""
        item = _ok(client.get(f"/api/v1/attempts/{sat[0]}/review",
                              headers=auth(seed["student"].xid)))["items"][0]
        assert "bus would have been faster" not in item["transcript_excerpt"]
        assert "university library" not in item["transcript_excerpt"]

    def test_a_reading_paper_has_neither(self, client, db, seed, published,
                                         student):
        """No audio, no range, no excerpt — and no crash looking for them."""
        xid, _ = _sit_and_submit(client, db, seed, published)
        item = _ok(client.get(f"/api/v1/attempts/{xid}/review",
                              headers=auth(seed["student"].xid)))["items"][0]
        assert item["audio_range"] is None
        assert item["transcript_excerpt"] is None
