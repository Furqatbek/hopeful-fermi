"""What every actor shares: a session, a clock, and the idempotency contract.

Actors are thin. The pattern throughout `app/workers` is:

    @dramatiq.actor(...)
    def some_job(arg):
        with unit_of_work() as session:
            some_module.do_the_thing(session, arg)

so the logic is testable by calling `do_the_thing` with a test session, and the
actor itself has nothing in it worth testing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from sqlalchemy.orm import Session

from app.platform.clock import Clock, SystemClock
from app.platform.db import unit_of_work as _unit_of_work

log = structlog.get_logger()


# One transaction per job, re-exported rather than reimplemented. This module
# used to carry its own copy of the body — identical to the platform one and to
# `app/api/deps.py`'s, three implementations of the boundary every other
# guarantee in this system sits on. "A partially applied regrade is worse than
# one that has to be retried, and retries are free because every actor is
# idempotent" is still the reason; it is just written in one place now.
unit_of_work = _unit_of_work


def clock() -> Clock:
    return SystemClock()


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@contextmanager
def advisory_lock(session: Session, key: str) -> Iterator[bool]:
    """A Postgres advisory lock, so two workers cannot run the same sweep at once.

    Used by the periodic jobs — refreshing a materialized view or matching a
    speaking slot twice concurrently is at best wasted work and at worst two sets
    of pairs for the same people. Transaction-scoped, so it is released by the
    commit or rollback above whatever happens to the process.

    Yields False rather than blocking when another worker holds it: a periodic
    job that is already running does not need a second copy queued behind it.
    """
    # A 64-bit key from the name, so callers pass a readable string.
    import hashlib

    from sqlalchemy import text

    digest = hashlib.sha256(key.encode()).digest()[:8]
    lock_id = int.from_bytes(digest, "big", signed=True)
    acquired = session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:k)").bindparams(k=lock_id))
    if not acquired:
        log.info("lock_busy", job=key)
    yield bool(acquired)
