"""The transactional outbox relay.

This is the ~80 lines that ADR-0001 §6 promised in place of Kafka. A domain event
is written to `outbox` in the SAME transaction as the change that caused it, so
it is impossible to have a regrade that happened without an event, or an event
for a regrade that rolled back. This process moves those rows onto the queue.

**Send, then mark.** Not the other way round. A crash between the two duplicates
the message; a crash the other way round loses it. Duplicate is recoverable —
every actor is idempotent — and lost is not. That single ordering decision is
what makes the whole scheme trustworthy, and it is why the idempotency rule in
`broker.py` is not negotiable.

**`FOR UPDATE SKIP LOCKED`.** Two relays can run without coordination and without
double-dispatch, which means the deploy story is "restart it" rather than "make
sure only one exists".

**Backoff, and then leave it alone.** A row that fails is rescheduled with
exponential backoff. After `MAX_ATTEMPTS` it stops being retried but is NOT
deleted or marked dispatched: it stays visible to the one query that matters.

    SELECT now() - min(created_at) FROM outbox WHERE dispatched_at IS NULL;

That is outbox lag, and per Deliverable 5 §5 it is the single best health signal
in the system — it covers regrade, notifications, analytics and webhooks at once.

**"The broker said no" is not poison.** The attempt budget exists for a message
the actors cannot route or decode — retrying that forever is what a dead-letter
query is for. A Redis that is restarting, out of memory or unreachable is a
different failure: it backs off the same way but never spends an attempt, so an
outage of any length leaves every row eligible for the moment the broker is
back. Before this, the ladder 2+4+...+128 s exhausted every pending row about
four minutes into an outage, and `outbox_lag_seconds` — which excludes exhausted
rows — went green over a queue that would never move again. That contradicted
the one promise `broker.py` makes: losing Redis loses queued work, never facts.
"""

from __future__ import annotations

import datetime as dt

import dramatiq.errors as dx
import redis.exceptions as rx
import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.platform import health

log = structlog.get_logger()

BATCH = 100
MAX_ATTEMPTS = 8
# 2s, 4s, 8s ... capped. Long enough that a flapping dependency is not hammered,
# short enough that a transient failure clears before anyone notices.
BACKOFF_BASE = 2
BACKOFF_CAP = dt.timedelta(minutes=10)

# What `actor.send` raises when the broker, not the message, is the problem.
# dramatiq 2.x does not wrap redis-py's errors in `enqueue`, so the redis classes
# are what actually arrive; `OutOfMemoryError` is the `noeviction` refusal the
# compose file configures, `BusyLoadingError` a Redis still reading its dump.
# The dramatiq family is for its own connection wrappers. Nothing else — a
# `KeyError` from the routing table or a `TypeError` from an argument builder is
# exactly the poison the budget is for.
TRANSIENT = (rx.ConnectionError, rx.TimeoutError, rx.OutOfMemoryError,
             rx.BusyLoadingError, dx.BrokerConnectionError)


def drain(session: Session, dispatch, *, batch: int = BATCH,
          now: dt.datetime | None = None) -> int:
    """Move one batch of undispatched events onto the queue.

    `dispatch(event_type, payload, aggregate_type, aggregate_id)` is injected so
    the relay can be tested without a broker: the test passes a recorder, and
    asserts on what would have been sent.
    """
    moment = now or dt.datetime.now(dt.UTC)
    rows = session.execute(text("""
        SELECT id, aggregate_type, aggregate_id, event_type, payload, attempts
        FROM outbox
        WHERE dispatched_at IS NULL
          AND available_at <= :now
          AND attempts < :max_attempts
        ORDER BY available_at, id
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    """).bindparams(now=moment, max_attempts=MAX_ATTEMPTS, batch=batch)
    ).mappings().all()

    dispatched = 0
    for row in rows:
        try:
            dispatch(row["event_type"], row["payload"] or {},
                     row["aggregate_type"], row["aggregate_id"])
        except Exception as exc:
            _reschedule(session, row, exc, moment)
            continue
        # Marked only after the send returned. The window between them is where
        # a duplicate can occur, and that is the correct side to fail on.
        session.execute(
            text("UPDATE outbox SET dispatched_at = :now, last_error = NULL "
                 "WHERE id = :id").bindparams(now=moment, id=row["id"]))
        dispatched += 1

    if rows:
        log.info("outbox_drained", found=len(rows), dispatched=dispatched)
    return dispatched


def _reschedule(session: Session, row, exc: Exception, moment: dt.datetime) -> None:
    """Push the row out by the backoff; charge the attempt budget only for poison.

    Classified BEFORE the budget is touched. The delay is always applied — a
    broker that is down is not helped by a hundred rows a second knocking on it —
    but `attempts` moves only when the failure is ours, so a transport outage can
    never cross the give-up line and the row is re-dispatched when Redis returns.
    """
    transient = isinstance(exc, TRANSIENT)
    attempts = row["attempts"] + (0 if transient else 1)
    delay = min(BACKOFF_CAP,
                dt.timedelta(seconds=BACKOFF_BASE ** max(1, row["attempts"] + 1)))
    session.execute(text("""
        UPDATE outbox
        SET attempts = :attempts, last_error = :error, available_at = :next
        WHERE id = :id
    """).bindparams(attempts=attempts, error=f"{type(exc).__name__}: {exc}"[:500],
                    next=moment + delay, id=row["id"]))
    log.warning("outbox_dispatch_failed", outbox_id=row["id"],
                event_type=row["event_type"], attempts=attempts,
                error=str(exc)[:200], transient=transient,
                # The alert threshold from Deliverable 5 §5.
                exhausted=attempts >= MAX_ATTEMPTS)


# Re-exported from the kernel: the API serves these at `/metrics/workers` and
# cannot import this package, so the queries live in `app.platform.health` and
# both sides read the same ones.
lag_seconds = health.outbox_lag_seconds
stuck = health.outbox_stuck


def purge_dispatched(session: Session, *, older_than: dt.timedelta) -> int:
    """Keep the table small so the pending-index stays hot.

    Dispatched rows are history, not state — the audit log is where "what
    happened" lives. A month is enough to debug last week's incident.
    """
    result = session.execute(text("""
        DELETE FROM outbox
        WHERE dispatched_at IS NOT NULL AND dispatched_at < now() - :age
    """).bindparams(age=older_than))
    return result.rowcount or 0
