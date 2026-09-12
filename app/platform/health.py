"""Operational readings, in the kernel because BOTH composition roots need them.

The API serves them at `/metrics/workers`; the worker loop logs them. Neither may
import the other (`pyproject.toml`, "The two composition roots are independent"),
so the queries live below both. They are raw SQL over shared tables and depend on
no module, which is exactly what belongs in `platform`.

**`outbox_lag_seconds` is the number.** Deliverable 5 §5: green under 5 s, page
over 60 s sustained. One query, and it covers regrade, notifications, analytics
projections and webhook retries at once — because everything asynchronous in this
system enters through the same table. Watch this instead of a dashboard.

**It cannot see a dead worker pool.** Lag measures relay-to-Redis: a row is
"dispatched" the moment it is on the queue, whether or not anything ever takes
it off. With the scheduler up and every `dramatiq` process dead, lag reads zero
while nothing is scored. `worker_heartbeat_age_seconds` is the other half —
read from the heartbeat set the broker's own Lua script maintains — and
`attempts_overdue` is the symptom a student would report. Page on any of the
three, not on lag alone.
"""

from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.orm import Session

# Matches `app.workers.relay.MAX_ATTEMPTS`. Duplicated as a constant rather than
# imported, because importing it here would make the kernel depend on the worker
# package and invert the layering this module exists to respect.
GIVE_UP_AFTER = 8

# `$namespace:__heartbeats__`, the sorted set dramatiq's Redis broker writes a
# worker id into (scored by epoch milliseconds) on every fetch. Named here rather
# than read off the broker for the same layering reason as `GIVE_UP_AFTER`.
HEARTBEATS_KEY = "dramatiq:__heartbeats__"
# A worker heartbeats on every fetch loop, ~1 s idle. Two minutes without one is
# a pool that is not consuming, allowing for a long `ingest_audio` on a single
# busy worker.
WORKER_DEAD_AFTER_MS = 120_000
# A regrade still `running` this long after it started is one whose worker died
# without recording it; `apply_regrade` writes `failed` when it can.
REGRADE_STALL = "2 hours"


def outbox_lag_seconds(session: Session) -> float:
    """Age of the oldest undispatched event. The single best worker-health
    signal in the system."""
    value = session.scalar(text("""
        SELECT EXTRACT(EPOCH FROM (now() - min(created_at)))
        FROM outbox WHERE dispatched_at IS NULL AND attempts < :max
    """).bindparams(max=GIVE_UP_AFTER))
    return float(value or 0)


def outbox_stuck(session: Session) -> int:
    """Events that will never be retried again without a human.

    Deliberately not deleted and not marked dispatched: a dead letter you cannot
    see is a dead letter you will not fix.
    """
    return int(session.scalar(text("""
        SELECT count(*) FROM outbox WHERE dispatched_at IS NULL AND attempts >= :max
    """).bindparams(max=GIVE_UP_AFTER)) or 0)


def worker_heartbeat(now_ms: int | None = None) -> dict:
    """Is anything consuming the queue? Read from the broker's own bookkeeping.

    Returns `workers_alive` (heartbeats younger than `WORKER_DEAD_AFTER_MS`) and
    `worker_heartbeat_age_seconds` (the freshest one, of any age). Both are
    `None` — not zero — when Redis cannot be asked, because "no workers" and
    "cannot tell" must not be the same reading on a dashboard, and because a
    Redis outage must not take the health endpoint down with it: only the call
    to Redis is inside the `try`. An empty set is a genuine zero with no age.

    `realtime.client` is used rather than a second client: same layer, and its
    one-second socket timeout is exactly the bound a health probe wants.
    """
    from app.platform import realtime

    moment = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        beats = realtime.client().zrange(HEARTBEATS_KEY, 0, -1, withscores=True)
    except Exception:                                      # noqa: BLE001 — see docstring
        return {"workers_alive": None, "worker_heartbeat_age_seconds": None}
    scores = [int(score) for _worker, score in beats]
    alive = sum(1 for score in scores if score >= moment - WORKER_DEAD_AFTER_MS)
    age = max(0, (moment - max(scores)) // 1000) if scores else None
    return {"workers_alive": alive, "worker_heartbeat_age_seconds": age}


def snapshot(session: Session, *, now_ms: int | None = None) -> dict:
    return {
        "outbox_lag_seconds": round(outbox_lag_seconds(session), 1),
        "outbox_stuck": outbox_stuck(session),
        **worker_heartbeat(now_ms),
        "notifications_queued": int(session.scalar(text(
            "SELECT count(*) FROM notifications WHERE status = 'queued'")) or 0),
        "notifications_failed": int(session.scalar(text(
            "SELECT count(*) FROM notifications WHERE status = 'failed'")) or 0),
        # Attempts past their deadline that the sweeper has not picked up. Nonzero
        # for more than a few minutes means the sweeper is not running, and a
        # student is sitting in front of an exam that will never be scored.
        "attempts_overdue": int(session.scalar(text("""
            SELECT count(*) FROM attempts
            WHERE status = 'in_progress' AND expires_at < now() - interval '5 minutes'
        """)) or 0),
        # A regrade whose apply raised. `apply_regrade` records it as `failed`
        # with the error in the report; a human decides whether to re-apply.
        "regrades_failed": int(session.scalar(text(
            "SELECT count(*) FROM regrade_jobs WHERE status = 'failed'")) or 0),
        # One that stopped without recording anything — the worker was killed
        # mid-apply, or the failure handler itself could not reach the database.
        # Before `regrades_failed` existed this was the only place such a job
        # lived: `running` forever, refused a re-apply, listed nowhere.
        "regrades_stalled": int(session.scalar(text("""
            SELECT count(*) FROM regrade_jobs
            WHERE status IN ('running', 'planning')
              AND coalesce(started_at, created_at) < now() - CAST(:stall AS interval)
        """).bindparams(stall=REGRADE_STALL)) or 0),
        "sms_cost_minor_this_month": int(session.scalar(text("""
            SELECT coalesce(sum(cost_minor), 0) FROM notifications
            WHERE channel = 'sms' AND sent_at >= date_trunc('month', now())
        """)) or 0),
    }
