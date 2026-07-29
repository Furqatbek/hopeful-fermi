"""Operational readings, in the kernel because BOTH composition roots need them.

The API serves them at `/metrics/workers`; the worker loop logs them. Neither may
import the other (`pyproject.toml`, "The two composition roots are independent"),
so the queries live below both. They are raw SQL over shared tables and depend on
no module, which is exactly what belongs in `platform`.

**`outbox_lag_seconds` is the number.** Deliverable 5 §5: green under 5 s, page
over 60 s sustained. One query, and it covers regrade, notifications, analytics
projections and webhook retries at once — because everything asynchronous in this
system enters through the same table. Watch this instead of a dashboard.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# Matches `app.workers.relay.MAX_ATTEMPTS`. Duplicated as a constant rather than
# imported, because importing it here would make the kernel depend on the worker
# package and invert the layering this module exists to respect.
GIVE_UP_AFTER = 8


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


def snapshot(session: Session) -> dict:
    return {
        "outbox_lag_seconds": round(outbox_lag_seconds(session), 1),
        "outbox_stuck": outbox_stuck(session),
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
        "sms_cost_minor_this_month": int(session.scalar(text("""
            SELECT coalesce(sum(cost_minor), 0) FROM notifications
            WHERE channel = 'sms' AND sent_at >= date_trunc('month', now())
        """)) or 0),
    }
