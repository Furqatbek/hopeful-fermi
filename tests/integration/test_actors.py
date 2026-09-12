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

import dramatiq
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
def isolated_uow(db, monkeypatch):
    """Like `in_session`, but each unit of work is a SAVEPOINT that rolls back
    on an exception — the one property of the real `unit_of_work` the tests
    below depend on. `in_session` flushes and never rolls back, so it cannot
    tell "one transaction per message" from "one transaction per batch", and
    it cannot show what a failed apply leaves behind.
    """
    @contextmanager
    def _uow():
        savepoint = db.begin_nested()
        try:
            yield db
            savepoint.commit()
        except BaseException:
            savepoint.rollback()
            raise

    monkeypatch.setattr(actors, "unit_of_work", _uow)

    @contextmanager
    def _lock(_session, _key):
        yield True

    monkeypatch.setattr(actors, "advisory_lock", _lock)
    return db


def user(db, name: str, telegram: int | None = None):
    from app.modules.identity.models import User

    row = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name=name,
               date_of_birth=dt.date(2000, 6, 1), telegram_user_id=telegram)
    db.add(row)
    db.flush()
    return row


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

    def test_a_regrade_whose_apply_raises_is_recorded_as_failed(
            self, isolated_uow, published, monkeypatch):
        """The rollback that protects `score_runs` also rolled back the
        `running` status the API set, so a job whose apply raised sat `running`
        forever — refused a re-apply, counted nowhere, visible nowhere. The
        failure is now written in a second transaction, merged INTO the report
        so the dry run's counts survive beside the reason, and re-raised so
        Dramatiq's one retry still happens."""
        from app.modules.exam import planner
        from app.modules.exam.models import RegradeJob

        job = RegradeJob(trigger="answer_key_change", subject_type="question_version",
                         subject_id=published["question_versions"][0].id,
                         initiated_by=published["author"].id, reason="fix",
                         status="running", started_at=dt.datetime.now(dt.UTC),
                         report={"attempts_total": 3, "bands_changed": 1})
        isolated_uow.add(job)
        isolated_uow.flush()
        runs_before = isolated_uow.scalar(text("SELECT count(*) FROM score_runs"))

        def explode(*_a, **_kw):
            raise RuntimeError("scorer fell over")

        monkeypatch.setattr(planner, "apply", explode)
        with pytest.raises(RuntimeError):
            actors.apply_regrade(str(job.xid))

        after = isolated_uow.execute(text("""
            SELECT status, finished_at, report FROM regrade_jobs WHERE id = :j
        """).bindparams(j=job.id)).mappings().one()
        assert after["status"] == "failed"
        assert after["finished_at"] is not None
        assert "scorer fell over" in after["report"]["error"]
        assert after["report"]["attempts_total"] == 3      # merged, not replaced
        assert isolated_uow.scalar(text("SELECT count(*) FROM score_runs")) == runs_before

    def test_a_failed_regrade_is_applied_again_on_the_retry(self, isolated_uow,
                                                            published):
        """`failed` is admitted by the status guard for exactly the one retry
        `max_retries=1` allows — otherwise recording the failure would have
        turned that retry into a silent no-op."""
        from app.modules.exam.models import RegradeJob

        job = RegradeJob(trigger="answer_key_change", subject_type="question_version",
                         subject_id=published["question_versions"][0].id,
                         initiated_by=published["author"].id, reason="fix",
                         status="failed", report={"error": "RuntimeError: earlier"})
        isolated_uow.add(job)
        isolated_uow.flush()

        actors.apply_regrade(str(job.xid))
        isolated_uow.refresh(job)
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

    def test_a_delivered_message_is_not_resent_when_a_later_one_fails(
            self, isolated_uow, monkeypatch):
        """One transaction PER MESSAGE.

        A Telegram POST cannot be rolled back with its row. Up to 200 sends used
        to share one unit of work, so a failure on the second row rolled back
        the `sent` mark on the first and the next tick sent it again. The
        failure here is one that escapes `deliver` itself — its own transport
        handling marks the row and continues, so a transport error is not the
        case this test is about.
        """
        from app.modules.identity import notify

        noon = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)      # 12:00 Tashkent
        first, second = user(isolated_uow, "First"), user(isolated_uow, "Second")
        notify.queue(isolated_uow, user_id=first.id, template="attempt.scored",
                     params={}, now=noon)
        notify.queue(isolated_uow, user_id=second.id, template="attempt.scored",
                     params={}, now=noon + dt.timedelta(minutes=1))

        class Recording(notify.Transport):
            delivered: list[int] = []

            def send(self, *, recipient, **_kw):
                self.delivered.append(recipient.user_id)
                return None

        monkeypatch.setattr(actors, "_transport", Recording)
        real_recipient = notify._recipient

        def database_hiccup(session, user_id):
            if user_id == second.id:
                raise RuntimeError("connection reset mid-batch")
            return real_recipient(session, user_id)

        monkeypatch.setattr(notify, "_recipient", database_hiccup)
        with pytest.raises(RuntimeError):
            actors.deliver_notifications()

        status_of = dict(isolated_uow.execute(text(
            "SELECT user_id, status FROM notifications")).all())
        assert status_of == {first.id: "sent", second.id: "queued"}
        assert Recording.delivered == [first.id]

        # The next tick, with the database back: the second goes, the first
        # does not go twice.
        monkeypatch.setattr(notify, "_recipient", real_recipient)
        actors.deliver_notifications()
        assert Recording.delivered == [first.id, second.id]
        assert isolated_uow.scalar(text(
            "SELECT count(*) FROM notifications WHERE status = 'sent'")) == 2

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


def every_actor() -> dict[str, dramatiq.Actor]:
    return {name: value for name, value in vars(actors).items()
            if isinstance(value, dramatiq.Actor)}


# Scheduler job name -> the actor `pass_once` sends for it.
PERIODIC = {"competitions": "tick_competitions", "speaking": "match_speaking",
            "notifications": "deliver_notifications", "sweep": "sweep",
            "analytics": "refresh_analytics"}


class TestScheduler:
    def test_every_scheduled_job_enqueues_something(self, monkeypatch):
        """A typo in the scheduler's job names would leave a whole subsystem
        never running, with no error anywhere.

        And every periodic actor carries a `max_age` of at least twice its
        interval: the scheduler enqueues whether or not a worker is consuming,
        so without one an outage leaves a backlog of identical ticks that are
        executed hours late on top of the live ones — and with a window
        shorter than the interval a merely slow worker would drop live ticks.
        """
        from app.workers import scheduler

        enqueued: list[str] = []
        for name in PERIODIC.values():
            actor = getattr(actors, name)
            monkeypatch.setattr(actor, "send",
                                lambda *a, _n=name: enqueued.append(_n))

        for job in scheduler.INTERVALS:
            if job == "relay":
                continue
            scheduler.pass_once(job)
        assert sorted(enqueued) == ["deliver_notifications", "match_speaking",
                                    "refresh_analytics", "sweep", "tick_competitions"]

        for job, name in PERIODIC.items():
            interval_ms = scheduler.INTERVALS[job].total_seconds() * 1000
            max_age = getattr(actors, name).options.get("max_age")
            assert max_age is not None, f"{name} has no max_age"
            assert max_age >= 2 * interval_ms, f"{name}: {max_age} < 2x {interval_ms}"

    def test_every_actor_is_enqueued_by_something(self, isolated_uow, published,
                                                  monkeypatch):
        """The inverse of the routing test: an actor that nothing sends to is a
        job that silently never runs. `refresh_leaderboard` was one — the live
        board stayed empty until the contest went final.

        Reachable means: routed from an outbox event, fanned out from one, sent
        by the scheduler, or sent by a periodic tick. The tick is driven at a
        wall-clock second inside its refresh window with one live contest.
        """
        from app.workers import scheduler

        reached: set[str] = set()
        for name, actor in every_actor().items():
            monkeypatch.setattr(actor, "send",
                                lambda *a, _n=name: reached.add(_n))

        for actor, _build in actors.ROUTES.values():
            if actor is not None:
                reached.add(actor.actor_name)
        for actor, _args in actors.fan_out("attempt.scored", {"attempt_id": 1}):
            reached.add(actor.actor_name)
        for job in scheduler.INTERVALS:
            if job != "relay":
                scheduler.pass_once(job)

        base = dt.datetime.now(dt.UTC).replace(microsecond=0)
        every = getattr(actors, "LEADERBOARD_EVERY", 15)
        in_window = base - dt.timedelta(seconds=int(base.timestamp()) % every)
        isolated_uow.execute(text("""
            INSERT INTO competitions (test_version_id, title, lobby_opens_at,
                                      starts_at, ends_at, duration_seconds, status,
                                      created_by)
            VALUES (:tv, 'Friday', :m - interval '10 min', :m - interval '5 min',
                    :m + interval '30 min', 1800, 'live', :by)
        """).bindparams(tv=published["test_version"].id, m=in_window,
                        by=published["author"].id))
        isolated_uow.flush()

        # Outside the window: a tick that asks for no refresh, so the board is
        # not rewritten on every five-second pass (ADR 0003's frame budget).
        monkeypatch.setattr(actors, "now",
                            lambda: in_window + dt.timedelta(seconds=7))
        actors.tick_competitions()
        assert "refresh_leaderboard" not in reached

        monkeypatch.setattr(actors, "now", lambda: in_window)
        actors.tick_competitions()

        assert reached == set(every_actor()), \
            f"unreachable: {sorted(set(every_actor()) - reached)}"

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
