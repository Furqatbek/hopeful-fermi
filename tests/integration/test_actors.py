"""The actors, called the way Dramatiq calls them.

This file exists because of one bug that bit twice.

Dramatiq serialises actor arguments to JSON, so an xid ALWAYS arrives as a
string — there is no FastAPI in the path to parse it into a `uuid.UUID`. Raw SQL
of the form `WHERE xid = :x` bound to a string raises
`operator does not exist: uuid = character varying`, which is invisible to every
unit test and to the pure-logic suites, and shows up only when a real message
reaches a real worker. Ten API endpoints had it; then two actors had it.

So every actor here is invoked with the exact argument types a JSON payload
produces, through the real routing table, against a real database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from app.workers import actors


@pytest.fixture
def in_session(db, monkeypatch):
    """Point the actors' unit of work at the test transaction.

    Actors normally open their own session; here they must join the test's, so
    assertions see what they wrote and the whole thing rolls back afterwards.
    """
    @contextmanager
    def _uow():
        yield db
        db.flush()

    monkeypatch.setattr(actors, "unit_of_work", _uow)

    @contextmanager
    def _lock(_session, _key):
        yield True

    monkeypatch.setattr(actors, "advisory_lock", _lock)
    return db


@pytest.fixture
def assignment(db, published):
    from app.modules.exam.models import Assignment, AssignmentTarget

    row = Assignment(org_id=published["org"].id,
                     test_version_id=published["test_version"].id,
                     assigned_by=published["author"].id, target_kind="users",
                     opens_at=dt.datetime.now(dt.UTC),
                     closes_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=2))
    db.add(row)
    db.flush()
    db.add(AssignmentTarget(assignment_id=row.id, user_id=published["student"].id))
    db.flush()
    return row


class TestActorsAcceptJsonArguments:
    """Every actor, called with the types a Dramatiq message actually carries."""

    def test_notify_assignment_takes_a_string_xid(self, in_session, assignment,
                                                  published):
        actors.notify_assignment(str(assignment.xid))
        assert in_session.scalar(text("""
            SELECT count(*) FROM notifications WHERE template = 'assignment.set'
        """)) == 1

    def test_notify_assignment_is_idempotent(self, in_session, assignment):
        actors.notify_assignment(str(assignment.xid))
        actors.notify_assignment(str(assignment.xid))
        assert in_session.scalar(text(
            "SELECT count(*) FROM notifications WHERE template = 'assignment.set'")) == 1

    def test_an_unknown_assignment_is_a_no_op_not_a_crash(self, in_session):
        """A redelivered event for something since deleted must not poison the
        queue with a permanently failing message."""
        actors.notify_assignment(str(uuid.uuid4()))

    def test_republish_competition_takes_a_string_xid(self, in_session):
        actors.republish_competition(str(uuid.uuid4()), "notice")

    def test_plan_regrade_takes_a_string_xid(self, in_session, published):
        from app.modules.exam.models import RegradeJob

        job = RegradeJob(trigger="answer_key_change", subject_type="question_version",
                         subject_id=published["question_versions"][0].id,
                         initiated_by=published["author"].id, reason="fix",
                         status="planning")
        in_session.add(job)
        in_session.flush()

        actors.plan_regrade(str(job.xid))
        in_session.refresh(job)
        assert job.status == "ready"

    def test_apply_regrade_ignores_a_job_that_already_completed(self, in_session,
                                                               published):
        """The redelivery guard. Without it a duplicated message rescores a
        finished job and churns `score_runs`."""
        from app.modules.exam.models import RegradeJob

        job = RegradeJob(trigger="answer_key_change", subject_type="question_version",
                         subject_id=published["question_versions"][0].id,
                         initiated_by=published["author"].id, reason="fix",
                         status="completed")
        in_session.add(job)
        in_session.flush()
        actors.apply_regrade(str(job.xid))
        in_session.refresh(job)
        assert job.status == "completed"

    def test_project_attempt_takes_an_integer(self, in_session, published, clock):
        from app.modules.exam.session import ExamSession
        from app.modules.qtypes.registry import default_scorer

        exam = ExamSession(in_session, default_scorer(), clock, grace_seconds=30)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.submit(attempt)

        actors.project_attempt(attempt.id)
        assert in_session.scalar(text(
            "SELECT count(*) FROM item_exposures WHERE attempt_id = :a"
        ).bindparams(a=attempt.id)) == 3

    def test_notify_scored_takes_an_integer(self, in_session, published, clock):
        from app.modules.exam.session import ExamSession
        from app.modules.qtypes.registry import default_scorer

        exam = ExamSession(in_session, default_scorer(), clock, grace_seconds=30)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.submit(attempt)

        actors.notify_scored(attempt.id)
        assert in_session.scalar(text(
            "SELECT count(*) FROM notifications WHERE template = 'attempt.scored'")) == 1

    def test_the_periodic_actors_run_against_an_empty_database(self, in_session):
        """Every scheduled job on a system with nothing in it.

        A tick that crashes when there is no work is a tick that crashes on the
        first morning after deployment, which is exactly when nobody is watching.
        """
        actors.tick_competitions()
        actors.match_speaking()
        actors.deliver_notifications()
        actors.sweep()

    def test_refresh_analytics_runs(self, db, monkeypatch):
        """Separate from the others: `REFRESH MATERIALIZED VIEW CONCURRENTLY`
        cannot run inside a transaction block, so this one commits."""
        @contextmanager
        def _uow():
            yield db
            db.commit()

        @contextmanager
        def _lock(_s, _k):
            yield True

        monkeypatch.setattr(actors, "unit_of_work", _uow)
        monkeypatch.setattr(actors, "advisory_lock", _lock)
        db.commit()
        actors.refresh_analytics()


class TestDispatchThroughTheRealRoutingTable:
    """The relay's own call path, so a route that names a missing actor fails."""

    @pytest.fixture
    def sent(self, monkeypatch):
        recorded: list[tuple[str, tuple]] = []

        for _name, (actor, _builder) in actors.ROUTES.items():
            if actor is None:
                continue
            monkeypatch.setattr(
                actor, "send",
                lambda *a, _n=actor.actor_name: recorded.append((_n, a)))
        for actor, _ in actors.fan_out("attempt.scored", {"attempt_id": 1}):
            monkeypatch.setattr(
                actor, "send",
                lambda *a, _n=actor.actor_name: recorded.append((_n, a)))
        return recorded

    def test_a_scored_attempt_fans_out_to_two_actors(self, sent):
        """Projection and notification must not be one actor: a failure in the
        delivery transport must not roll back the exposure record."""
        actors.dispatch_all("attempt.scored", {"attempt_id": 42}, "attempt", "x")
        assert sorted(name for name, _ in sent) == ["notify_scored", "project_attempt"]
        assert all(args == (42,) for _, args in sent)

    def test_a_regrade_plan_request_routes_with_its_xid(self, sent):
        job_xid = str(uuid.uuid4())
        actors.dispatch_all("regrade.plan_requested", {"regrade_job_xid": job_xid},
                            "regrade_job", job_xid)
        assert sent == [("plan_regrade", (job_xid,))]

    def test_a_republish_carries_the_public_notice(self, sent):
        xid = str(uuid.uuid4())
        actors.dispatch_all("competition.regrade_approved",
                            {"competition_xid": xid, "public_notice": "Key fixed."},
                            "competition", xid)
        assert sent == [("republish_competition", (xid, "Key fixed."))]

    def test_a_known_event_with_no_handler_is_silent(self, sent):
        actors.dispatch_all("attempt.started", {"attempt_id": 1}, "attempt", "x")
        assert sent == []


class TestScheduler:
    def test_every_scheduled_job_enqueues_something(self, monkeypatch):
        """A typo in the scheduler's job names would leave a whole subsystem
        never running, with no error anywhere."""
        from app.workers import scheduler

        enqueued: list[str] = []
        for name in ("tick_competitions", "match_speaking", "deliver_notifications",
                     "sweep", "refresh_analytics"):
            actor = getattr(actors, name)
            monkeypatch.setattr(actor, "send",
                                lambda *a, _n=name: enqueued.append(_n))

        for job in scheduler.INTERVALS:
            if job == "relay":
                continue
            scheduler.pass_once(job)
        assert sorted(enqueued) == ["deliver_notifications", "match_speaking",
                                    "refresh_analytics", "sweep", "tick_competitions"]

    def test_an_unknown_job_name_raises(self):
        from app.workers import scheduler

        with pytest.raises(KeyError):
            scheduler.pass_once("nonsense")

    def test_a_failing_job_does_not_stop_the_loop(self, monkeypatch):
        """A relay that dies because the analytics refresh raised is a system
        where a reporting bug silently stops student notifications."""
        from app.workers import scheduler

        def explode(_job):
            raise RuntimeError("boom")

        monkeypatch.setattr(scheduler, "pass_once", explode)
        scheduler._safely("analytics")          # must not raise
