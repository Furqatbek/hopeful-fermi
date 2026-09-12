"""Housekeeping. Boring, and the reason the system still works in six months.

Five jobs, in order of how badly their absence hurts:

  1. **Auto-submit expired attempts.** Without it a student who closes the tab
     mid-exam has an attempt stuck `in_progress` forever, is never scored, and
     shows as "not started" on their teacher's dashboard. This is the one that
     produces support tickets on day one.
  2. **Create next month's partitions.** `audit_log`, `item_exposures` and
     `attempt_answer_events` are partitioned by month and the migrations created
     three. There is a DEFAULT partition so nothing FAILS when they run out —
     rows land there and the table quietly stops being partitioned, which is a
     performance cliff nobody notices until it is a year deep.
  3. **Abandon stale uploads.** A dropped 40 MB upload holds a row and,
     eventually, object storage.
  4. **Expire idempotency keys.** The table is written on every mutating request
     and read by index; left alone it grows without bound.
  5. **Purge dispatched outbox rows.** Keeps the pending-index small, which keeps
     the lag query — the one number worth alerting on — instant.
"""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

log = structlog.get_logger()

PARTITIONED = ("audit_log", "item_exposures", "attempt_answer_events")
# How far ahead to keep partitions. Two months means a sweeper that has been down
# for four weeks still has somewhere to put rows.
PARTITION_LEAD = 2
OUTBOX_RETENTION = dt.timedelta(days=30)
UPLOAD_TTL = dt.timedelta(hours=24)


def run(session: Session, now: dt.datetime) -> dict[str, int]:
    counts = {
        "attempts_submitted": len(auto_submit(session, now)),
        "partitions_created": ensure_partitions(session, now),
        "uploads_abandoned": abandon_uploads(session, now),
        "idempotency_expired": expire_idempotency(session, now),
        "outbox_purged": purge_outbox(session),
        "otp_expired": expire_otp(session),
    }
    log.info("sweep", **counts)
    return counts


def auto_submit(session: Session, now: dt.datetime) -> list[int]:
    """Score whatever ran out of time.

    Delegated to `ExamSession` rather than re-implemented: an auto-submit that
    scored differently from a manual one would be a two-implementation scoring
    bug of exactly the kind the whole design is arranged to prevent.
    """
    from app.modules.exam.session import ExamSession
    from app.modules.qtypes.registry import default_scorer, refresh_from_db
    from app.platform.clock import SystemClock
    from app.platform.config import settings

    # The third composition root that scores, after the API (`deps.scorer`) and
    # the regrade actors (`actors._scorer`), and it was the one that never
    # refreshed. A question type registered through `POST /admin/question-types`
    # was a guaranteed `RegistryError` in every sweep of a fresh worker — which,
    # before `auto_submit_expired` savepointed each attempt, rolled back the
    # entire sweep. Same rule as the other two: the database's definitions win.
    refresh_from_db(session)
    exam = ExamSession(session, default_scorer(), SystemClock(),
                       grace_seconds=settings().submit_grace_seconds)
    return exam.auto_submit_expired()


def ensure_partitions(session: Session, now: dt.datetime) -> int:
    """Create next month's partitions before they are needed.

    Idempotent through `IF NOT EXISTS`, so running it hourly costs one catalogue
    lookup and running it never costs a silent performance cliff.
    """
    created = 0
    for offset in range(PARTITION_LEAD + 1):
        start = _month_start(now, offset)
        end = _month_start(now, offset + 1)
        for table in PARTITIONED:
            name = f"{table}_{start:%Y_%m}"
            exists = session.scalar(
                text("SELECT to_regclass(:n) IS NOT NULL").bindparams(n=name))
            if exists:
                continue
            session.execute(text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
                f"FOR VALUES FROM ('{start:%Y-%m-%d}') TO ('{end:%Y-%m-%d}')"))
            created += 1
            log.info("partition_created", table=table, partition=name)
    return created


def _month_start(now: dt.datetime, offset: int) -> dt.date:
    year, month = now.year, now.month + offset
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return dt.date(year, month, 1)


def abandon_uploads(session: Session, now: dt.datetime) -> int:
    result = session.execute(text("""
        UPDATE media_uploads SET status = 'aborted'
        WHERE status = 'open' AND expires_at < :now
    """).bindparams(now=now))
    return result.rowcount or 0


def expire_idempotency(session: Session, now: dt.datetime) -> int:
    result = session.execute(text(
        "DELETE FROM idempotency_keys WHERE expires_at < :now").bindparams(now=now))
    return result.rowcount or 0


def expire_otp(session: Session) -> int:
    """Consumed and expired codes, gone after a day.

    A table of hashed login codes is a table worth not keeping. The hash means a
    leak is not immediately usable; deleting them means it is not a question.
    """
    result = session.execute(text("""
        DELETE FROM otp_challenges
        WHERE created_at < now() - interval '1 day'
    """))
    return result.rowcount or 0


def purge_outbox(session: Session) -> int:
    from app.workers import relay

    return relay.purge_dispatched(session, older_than=OUTBOX_RETENTION)


def health(session: Session, *, now_ms: int | None = None) -> dict:
    """The numbers a monitoring check reads. Owned by `app.platform.health` so
    the API can serve them without importing this package."""
    from app.platform import health as kernel

    return kernel.snapshot(session, now_ms=now_ms)
