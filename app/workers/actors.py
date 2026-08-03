"""The Dramatiq actors. Thin by rule: open a transaction, call a module, done.

Nothing here has logic worth testing, which is the point — the logic lives in
`app/modules/*` and is tested by calling those functions with a session. What is
tested here is the routing table at the bottom, because a typo in an event name
is a job that silently never runs.

Every actor is idempotent, because the relay delivers at-least-once. Where that
is not free, the guard is named in the actor.
"""

from __future__ import annotations

from typing import Any

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
    """The delivery adapter. Telegram, over the bot token this deployment already
    configures for Mini App sign-in.

    This returned a logging stub, so every notification reached `status = 'sent'`
    having been written to a log file and nowhere else. SMS is still not
    implemented and fails closed with a reason on the row — see
    `identity/transport.py` for why no provider is contracted.
    """
    from app.modules.identity.transport import TelegramTransport
    from app.platform.config import settings

    return TelegramTransport(settings().telegram_bot_token)


# ── media ────────────────────────────────────────────────────────────

# Its own queue, and the reason is Deliverable 5 §5: transcoding is the one CPU
# hog in this system — a 30-minute WAV is 30-60 s of ffmpeg. On a shared box it
# will happily starve the web workers, so it runs `nice`d (in platform.audio) and
# on a queue that can be given a single dedicated worker with `--queues media`.
@dramatiq.actor(queue_name="media", max_retries=2, time_limit=1_800_000)
def ingest_audio(media_asset_id: int) -> None:
    """Probe, validate, normalise loudness, encode, store the delivery file.

    Idempotent by status guard: an asset already `ready` returns immediately,
    which matters because the relay is at-least-once and re-encoding a
    thirty-minute file costs a minute of CPU every time.

    A file that fails VALIDATION is not an error here — it is content feedback,
    recorded on the asset and shown to the author. Only an unexpected failure
    raises and gets retried.
    """
    from app.modules.content import media as media_service
    from app.platform.storage import scratch_dir, storage

    with unit_of_work() as session:
        media_service.ingest_audio(session, storage(), media_asset_id,
                                   now=now(), scratch=scratch_dir())


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

    moved: list[dict] = []
    with unit_of_work() as session:
        with advisory_lock(session, "competitions.tick") as acquired:
            if not acquired:
                return
            moved = service.tick(session, now())

    # AFTER the commit, deliberately, and this is the one publish in the system
    # that is not driven by the outbox. A state change is a fact about a contest
    # 200 people are watching a countdown for; announcing one that then rolled
    # back would start every client's timer against a lobby that never opened.
    # Losing one is survivable — the tick runs every five seconds and the next
    # pass finds the same status — which is what makes "after commit, no outbox
    # row" the right trade here and the wrong one for a speaking match.
    for change in moved:
        _publish(f"competition:{change['competition_xid']}", "competition.state",
                 {"status": change["to"], "server_now": now().isoformat(),
                  "seconds_to_start": None})


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
    "media.uploaded": (ingest_audio, lambda p: (p["media_asset_id"],)),
    # No dramatiq actor, on purpose: these four drive the realtime bus and
    # nothing else. Listed so the relay treats them as known — an event missing
    # from this table is retried eight times and dead-lettered.
    #
    # `attempt.started` really does nothing yet. The comment here used to claim
    # `attempt.expired` was emitted too; it was not, by anything, which is why
    # `RtAttemptForceSubmit` had no producer. `ExamSession.submit` emits it now.
    "attempt.started": (None, None),
    "attempt.expired": (None, None),
    "speaking.matched": (None, None),
    "identity.session_revoked": (None, None),
}


# ── the realtime projection of the outbox ────────────────────────────

# Outbox event type -> the realtime frames it becomes. Deliberately the SAME
# rows, drained by the SAME relay, rather than a second event pipeline: a
# realtime frame for a pair that rolled back, or a pair with no frame, is exactly
# what the transactional outbox exists to make unrepresentable (ADR-0001 §6).
#
# A builder is a pure function of `(payload, aggregate_id)` and returns
# `(channel, event_type, data)` triples. Pure because the relay holds a
# transaction open while it runs, and a builder that went back to the database
# would be doing it on that transaction, from a loop over a hundred rows.
# Everything a frame needs is therefore put into the payload by whoever emitted
# it — including public xids, because internal integer ids are never exposed.
def _speaking_matched(payload: dict, aggregate_id: str) -> list[tuple[str, str, dict]]:
    """One frame per peer, on their own personal channel.

    NOT one frame on `slot:{xid}`: `RtQueueMatched.initiator` says "exactly one
    peer is told to create the offer", so the frame differs per recipient, and a
    shared channel would either broadcast who was paired with whom to everyone
    booked on the slot or need per-recipient filtering in the bus — which is the
    second permission model `authz/channels.py` exists to avoid.

    Nothing here re-derives who may be paired with whom. Age banding is settled
    in `speaking.matching`, asserted again on the INSERT, and this is downstream
    of both: it addresses a pair that already exists.
    """
    slot_xid = payload.get("slot_xid")
    event = "slot.matched" if slot_xid else "queue.matched"
    initiator = str(payload.get("initiator_user_id"))
    frames = []
    for user_id, user_xid in (payload.get("user_xids") or {}).items():
        frames.append((f"user:{user_xid}", event, {
            "pair": {"xid": payload["pair_xid"], "origin": payload.get("origin"),
                     "age_band": payload.get("age_band"), "slot_xid": slot_xid},
            "initiator": user_id == initiator,
        }))
    return frames


def _attempt_expired(payload: dict, aggregate_id: str) -> list[tuple[str, str, dict]]:
    return [(f"attempt:{aggregate_id}", "attempt.force_submit",
             {"attempt_xid": aggregate_id, "reason": payload.get("reason", "expired")})]


def _session_revoked(payload: dict, aggregate_id: str) -> list[tuple[str, str, dict]]:
    return [(f"user:{payload['user_xid']}", "session.revoked",
             {"reason": payload.get("reason", "banned"), "until": payload.get("until")})]


REALTIME: dict[str, Any] = {
    "speaking.matched": _speaking_matched,
    "attempt.expired": _attempt_expired,
    "identity.session_revoked": _session_revoked,
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


def broadcast(event_type: str, payload: dict, aggregate_id: str) -> int:
    """Project one outbox event onto the realtime bus. Returns frames published.

    **Never raises.** A Redis outage must not fail the dispatch, because the
    relay treats a failed dispatch as "retry this row" — so a realtime publish
    that raised would stall the entire outbox, and with it regrades, analytics
    and every notification, for as long as Redis was unreachable. That is the one
    place in this design where the bus is allowed to fail silently, and it is
    survivable for the reason §4.7 gives: every frame here has an HTTP mirror the
    client falls back to. Logged at WARNING, because a bus that has been down for
    a month must not look like a quiet one.
    """
    build = REALTIME.get(event_type)
    if build is None:
        return 0
    return sum(_publish(channel, rt_type, data)
               for channel, rt_type, data in build(payload, aggregate_id))


def dispatch_all(event_type: str, payload: dict, aggregate_type: str,
                 aggregate_id: str) -> None:
    dispatch(event_type, payload, aggregate_type, aggregate_id)
    for actor, args in fan_out(event_type, payload):
        actor.send(*args)
    broadcast(event_type, payload, aggregate_id)


def _publish(channel: str, event_type: str, data: dict) -> int:
    """The one place this process puts a frame on the bus.

    `assert_carries` first, and deliberately OUTSIDE the `try`: a Redis outage is
    an operational failure to swallow and log, while an event addressed to a
    channel family that does not declare it is a bug in the routing table above.
    Swallowing the second would deliver a teacher's invigilation view to whatever
    channel the typo named.
    """
    from app.modules.authz import channels
    from app.platform import realtime

    channels.assert_carries(channel, event_type)
    try:
        realtime.publish(channel, event_type, data)
    except Exception as exc:                       # noqa: BLE001 — see `broadcast`
        # `rt_type=`, not `event=`: structlog's first positional argument IS
        # `event`, so a keyword of that name raises `TypeError` from inside the
        # handler for a Redis outage — turning a logged failure into a crash on
        # the relay's thread. Found by the test that runs this path with Redis
        # actually unreachable, which is the only way it can be found.
        log.warning("realtime_publish_failed", channel=channel,
                    rt_type=event_type, error=str(exc)[:200])
        return 0
    return 1
