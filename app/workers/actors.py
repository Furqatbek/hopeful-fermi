"""The Dramatiq actors. Thin by rule: open a transaction, call a module, done.

Nothing here has logic worth testing, which is the point — the logic lives in
`app/modules/*` and is tested by calling those functions with a session. What is
tested here is the routing table at the bottom, because a typo in an event name
is a job that silently never runs.

Every actor is idempotent, because the relay delivers at-least-once. Where that
is not free, the guard is named in the actor.
"""

from __future__ import annotations

import dramatiq
import structlog

from app.workers import broker
from app.workers.runtime import advisory_lock, now, unit_of_work

log = structlog.get_logger()

# BEFORE the first `@dramatiq.actor` below, and that ordering is load-bearing.
# The decorator binds `dramatiq.get_broker()` at decoration time, and
# `get_broker()` invents a RedisBroker on localhost:6379 when none is installed —
# so importing this module first would bind every actor to the wrong Redis and
# the only symptom would be messages that silently go nowhere.
broker.current()

# Retries are the actor's own, on top of the relay's. The relay retries the
# DISPATCH; Dramatiq retries the EXECUTION. Both are needed: a broker hiccup and
# a deadlocked transaction are different failures.
RETRY = {"max_retries": 5, "min_backoff": 2_000, "max_backoff": 300_000}


def _scorer():
    from app.modules.qtypes.registry import default_scorer

    return default_scorer()


# ── regrade ──────────────────────────────────────────────────────────

@dramatiq.actor(queue_name="regrade", max_retries=3, time_limit=1_800_000)
def plan_regrade(regrade_job_xid: str) -> None:
    """Dry run. Idempotent because it only ever writes counts onto the job, and
    recomputing produces the same counts."""
    from app.modules.exam import planner
    from app.modules.exam.models import RegradeJob

    with unit_of_work() as session:
        job = _job(session, RegradeJob, regrade_job_xid)
        if job is None or job.status not in ("planning", "ready"):
            return
        planner.plan(session, job, _scorer())


@dramatiq.actor(queue_name="regrade", max_retries=1, time_limit=3_600_000)
def apply_regrade(regrade_job_xid: str) -> None:
    """Commit.

    `max_retries=1` deliberately, against the grain of everything else here. A
    regrade that fails halfway leaves some students rescored and some not, and
    the right response to that is a human looking at it — not four more automatic
    attempts churning `score_runs` while they sleep. The status guard below makes
    the one retry safe.
    """
    from app.modules.exam import planner
    from app.modules.exam.models import RegradeJob

    with unit_of_work() as session:
        job = _job(session, RegradeJob, regrade_job_xid)
        if job is None or job.status not in ("running", "ready"):
            return                      # already completed: this is a redelivery
        planner.apply(session, job, _scorer(), now=now())


@dramatiq.actor(queue_name="regrade", max_retries=1, time_limit=3_600_000)
def republish_competition(competition_xid: str, public_notice: str) -> None:
    """Rewrite a finished board after a recorded admin decision."""
    from sqlalchemy import text

    from app.modules.competitions import service

    with unit_of_work() as session:
        competition_id = session.scalar(
            text("SELECT id FROM competitions WHERE xid = CAST(:x AS uuid)")
            .bindparams(x=competition_xid))
        if competition_id is None:
            return
        service.republish(session, competition_id, now=now(),
                          public_notice=public_notice)


# ── notifications ────────────────────────────────────────────────────

@dramatiq.actor(queue_name="notify", **RETRY)
def notify_assignment(assignment_xid: str) -> None:
    """One notice per targeted student. Deduplicated on
    `assignment:<id>:<user>`, so a redelivery is silent."""
    from sqlalchemy import text

    from app.modules.identity import notify

    with unit_of_work() as session:
        row = session.execute(text("""
            SELECT a.id, a.opens_at, a.closes_at, tv.title
            FROM assignments a JOIN test_versions tv ON tv.id = a.test_version_id
            WHERE a.xid = CAST(:x AS uuid)
        """).bindparams(x=assignment_xid)).mappings().first()
        if row is None:
            return
        targets = session.execute(text(
            "SELECT user_id FROM assignment_targets WHERE assignment_id = :a"
        ).bindparams(a=row["id"])).scalars().all()
        for user_id in targets:
            notify.queue(session, user_id=user_id, template="assignment.set",
                         params={"assignment_xid": assignment_xid,
                                 "test_title": row["title"],
                                 "closes_at": row["closes_at"].isoformat()},
                         dedupe_key=f"assignment:{row['id']}:{user_id}")


@dramatiq.actor(queue_name="notify", **RETRY)
def notify_scored(attempt_id: int) -> None:
    from sqlalchemy import text

    from app.modules.identity import notify

    with unit_of_work() as session:
        row = session.execute(text("""
            SELECT a.user_id, a.xid, a.mode, r.band, r.id AS run_id
            FROM attempts a JOIN score_runs r ON r.attempt_id = a.id AND r.is_current
            WHERE a.id = :a
        """).bindparams(a=attempt_id)).mappings().first()
        if row is None or row["mode"] == "preview":
            return
        notify.queue(session, user_id=row["user_id"], template="attempt.scored",
                     params={"attempt_xid": str(row["xid"]),
                             "band": float(row["band"]) if row["band"] else None},
                     dedupe_key=f"scored:{attempt_id}:{row['run_id']}")


@dramatiq.actor(queue_name="notify", **RETRY)
def deliver_notifications(limit: int = 200) -> None:
    from app.modules.identity import notify

    with unit_of_work() as session:
        notify.deliver(session, _transport(), now=now(), limit=limit)


def _transport():
    """The delivery adapter. A logging stub until a provider is contracted; the
    queue, the retries and the cost accounting are real either way."""
    from app.modules.identity.notify import Transport

    return Transport()


# ── analytics ────────────────────────────────────────────────────────

@dramatiq.actor(queue_name="analytics", **RETRY)
def project_attempt(attempt_id: int) -> None:
    """Everything that follows from one attempt being scored.

    Exposure first: it is the record that an item was put in front of a student,
    and it is what the burn score is built from.
    """
    from app.modules.analytics import projections

    with unit_of_work() as session:
        projections.record_exposure(session, attempt_id)


@dramatiq.actor(queue_name="analytics", time_limit=900_000, **RETRY)
def refresh_analytics() -> None:
    """The periodic sweep. Guarded by an advisory lock so an overlapping tick is
    a no-op rather than two concurrent view refreshes."""
    from app.modules.analytics import projections

    with unit_of_work() as session:
        with advisory_lock(session, "analytics.refresh") as acquired:
            if not acquired:
                return
            moment = now()
            projections.refresh_attendance(session, now=moment)
            projections.refresh_item_stats(session, now=moment)
            projections.refresh_exposure(session, now=moment)
            projections.refresh_user_progress(session, now=moment)
            # Last, because it is the one that takes a lock other things wait on.
            projections.refresh_cohort_progress(session)


# ── competitions and speaking ────────────────────────────────────────

@dramatiq.actor(queue_name="scheduler", **RETRY)
def tick_competitions() -> None:
    from app.modules.competitions import service

    with unit_of_work() as session:
        with advisory_lock(session, "competitions.tick") as acquired:
            if not acquired:
                return
            service.tick(session, now())


@dramatiq.actor(queue_name="scheduler", **RETRY)
def refresh_leaderboard(competition_id: int) -> None:
    """The live board during a contest. Provisional by definition — a rank shown
    while people are still submitting is a snapshot, and saying so is what stops
    it being read as a result."""
    from sqlalchemy import text

    from app.modules.competitions import service

    with unit_of_work() as session:
        tiebreak = session.scalar(
            text("SELECT tiebreak FROM competitions WHERE id = :c AND status = 'live'")
            .bindparams(c=competition_id))
        if tiebreak is None:
            return
        service.materialize(session, competition_id, tiebreak=tiebreak, now=now(),
                            provisional=True)


@dramatiq.actor(queue_name="scheduler", **RETRY)
def match_speaking() -> None:
    """Both matchers on one tick: slots that have opened, then the live queue."""
    from app.modules.speaking import service

    with unit_of_work() as session:
        with advisory_lock(session, "speaking.match") as acquired:
            if not acquired:
                return
            moment = now()
            for slot_id in service.due_slots(session, moment):
                service.match_slot(session, slot_id, moment)
            service.match_queue(session, moment)


# ── sweeper ──────────────────────────────────────────────────────────

@dramatiq.actor(queue_name="scheduler", time_limit=600_000, **RETRY)
def sweep() -> None:
    from app.workers import sweeper

    with unit_of_work() as session:
        with advisory_lock(session, "sweeper") as acquired:
            if not acquired:
                return
            sweeper.run(session, now())


def _job(session, model, xid: str):
    import uuid

    from sqlalchemy import select

    return session.scalars(select(model).where(model.xid == uuid.UUID(xid))).first()


# ── the routing table ────────────────────────────────────────────────

# Outbox event type → the actor that handles it, and how to build its arguments
# from the event payload. An event with no entry is dropped by the relay with a
# warning rather than silently, because "the job never ran" is otherwise
# indistinguishable from "the job ran and did nothing".
ROUTES: dict[str, tuple] = {
    "regrade.plan_requested": (plan_regrade,
                               lambda p: (p["regrade_job_xid"],)),
    "regrade.apply_requested": (apply_regrade,
                                lambda p: (p["regrade_job_xid"],)),
    "competition.regrade_approved": (republish_competition,
                                     lambda p: (p["competition_xid"],
                                                p.get("public_notice") or "")),
    "assignment.created": (notify_assignment,
                           lambda p: (p["assignment_xid"],)),
    "attempt.scored": (project_attempt, lambda p: (p["attempt_id"],)),
    # `attempt.started` and `attempt.expired` are emitted and deliberately have
    # no handler yet. Listed so the relay treats them as known and does not warn.
    "attempt.started": (None, None),
    "attempt.expired": (None, None),
}


def dispatch(event_type: str, payload: dict, aggregate_type: str,
             aggregate_id: str) -> None:
    """What the relay calls. Raises on an unknown event so the row is retried and
    then surfaces in the dead-letter query rather than vanishing."""
    if event_type not in ROUTES:
        raise KeyError(f"no worker route for event {event_type!r}")
    actor, build_args = ROUTES[event_type]
    if actor is None:
        return
    actor.send(*build_args(payload))


def fan_out(event_type: str, payload: dict) -> list:
    """Events that drive more than one actor.

    Scoring an attempt both projects analytics and notifies the student, and
    those must not be one actor: a failure in the notification transport must not
    roll back the exposure record.
    """
    extra = []
    if event_type == "attempt.scored":
        extra.append((notify_scored, (payload["attempt_id"],)))
    return extra


def dispatch_all(event_type: str, payload: dict, aggregate_type: str,
                 aggregate_id: str) -> None:
    dispatch(event_type, payload, aggregate_type, aggregate_id)
    for actor, args in fan_out(event_type, payload):
        actor.send(*args)
