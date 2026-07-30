#!/usr/bin/env python3
"""Boot a real worker against a real Redis and prove a job actually runs.

**Nothing in the test suite covers this.** `tests/integration/test_actors.py`
calls the actor functions directly and checks the routing table, which is the
right thing to test in-process — but it cannot see the failure mode that
actually happened during the worker phase:

    `@dramatiq.actor` binds `dramatiq.get_broker()` AT DECORATION TIME, and
    `get_broker()` invents a RedisBroker on localhost:6379 when none is
    installed. Import `app.workers.actors` before `broker.configure()` and every
    actor is permanently bound to the wrong Redis. Messages go nowhere. Nothing
    raises. The queue is simply empty.

`actors.py` guards it by calling `broker.current()` above its first decorator,
and the guard is one line that anybody could reorder. The only way to catch a
regression is to run the real thing: enqueue here, and watch a separate OS
process pick it up over a real socket.

Two paths, both required:

  1. **The relay.** An outbox row written in a transaction reaches the queue.
     This is the ~80 lines that ADR-0001 section 6 offers in place of Kafka, so
     "it works" should not be taken on trust.
  2. **The worker.** A message enqueued in this process is executed by a
     `dramatiq` process that was started separately, and its write lands in
     PostgreSQL.

    TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost/postgres \\
    REDIS_URL=redis://localhost:6379/15 python3 scripts/smoke_workers.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
BOOT_TIMEOUT = 45.0
WORK_TIMEOUT = 45.0
POLL = 0.25


def main() -> int:
    base = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not base:
        sys.exit("set TEST_DATABASE_URL (or DATABASE_URL)")
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        sys.exit("set REDIS_URL — this smoke test exists to exercise a real one")

    parsed = make_url(base)
    name = f"ielts_smoke_{uuid.uuid4().hex[:8]}"
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = parsed.set(database=name).render_as_string(hide_password=False)
    # The worker resolves its own settings from the environment, so this is also
    # the assertion that a worker configured the way we document it works.
    env = {**os.environ, "DATABASE_URL": scratch, "REDIS_URL": redis_url}

    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    worker = None
    try:
        if subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                          cwd=ROOT, env=env, capture_output=True).returncode != 0:
            return _fail("could not migrate the scratch database")

        engine = create_engine(scratch)
        _flush_queue(redis_url)
        user_id = _seed_user(engine)

        worker = subprocess.Popen(
            [sys.executable, "-m", "dramatiq", "app.workers.actors",
             "--processes", "1", "--threads", "2", "--queues", "notify"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        if not _await_boot(worker):
            return _fail("the worker did not report ready", worker)
        print("  worker       booted against the configured Redis")

        problems = []
        problems += _relay_reaches_the_queue(engine, env)
        problems += _worker_executes_a_job(engine, env, user_id, worker)
        problems += _the_configured_redis_was_the_one_used(redis_url)

        if problems:
            print()
            for p in problems:
                print(f"  FAIL  {p}")
            print(_worker_log(worker))
            return 1
    finally:
        if worker is not None and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()

    print("PASS  outbox -> relay -> Redis -> worker process -> PostgreSQL")
    return 0


def _relay_reaches_the_queue(engine, env) -> list[str]:
    """An outbox row committed with the domain change reaches the broker.

    Run in-process with the real `dispatch`, so a route missing from `ROUTES` or
    an actor bound to the wrong broker both surface here.
    """
    sys.path.insert(0, str(ROOT))
    os.environ["REDIS_URL"] = env["REDIS_URL"]
    os.environ["DATABASE_URL"] = env["DATABASE_URL"]
    from sqlalchemy.orm import Session

    from app.workers import actors, relay

    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO outbox (event_type, aggregate_type, aggregate_id, payload)
            VALUES ('attempt.started', 'attempt', '1', '{"attempt_id": 1}'::jsonb)
        """))
    with Session(engine) as session:
        moved = relay.drain(session, actors.dispatch_all)
        session.commit()

    if moved != 1:
        return [f"the relay moved {moved} rows, expected 1"]
    with engine.connect() as c:
        undispatched = c.scalar(text(
            "SELECT count(*) FROM outbox WHERE dispatched_at IS NULL"))
    if undispatched:
        return ["the relay reported success but left the row undispatched"]
    print("  relay        outbox row dispatched and marked")
    return []


def _worker_executes_a_job(engine, env, user_id: int, worker) -> list[str]:
    """The half that needs a second process.

    A notification is queued directly in the database and the actor that drains
    them is enqueued over Redis. If the actor bound to the wrong broker, the
    message is accepted here and never arrives — which is exactly the silent
    failure this script exists for, and why it times out rather than hanging.
    """
    from app.workers.actors import deliver_notifications

    with engine.begin() as c:
        notification_id = c.scalar(text("""
            INSERT INTO notifications (user_id, channel, template, params, locale)
            VALUES (:u, 'in_app', 'smoke.probe', '{}'::jsonb, 'uz-Latn')
            RETURNING id
        """).bindparams(u=user_id))

    deliver_notifications.send()

    deadline = time.monotonic() + WORK_TIMEOUT
    status = "queued"
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            return [f"the worker exited with code {worker.returncode} mid-job"]
        with engine.connect() as c:
            status = c.scalar(text(
                "SELECT status FROM notifications WHERE id = :i"
            ).bindparams(i=notification_id))
        if status != "queued":
            break
        time.sleep(POLL)

    if status == "queued":
        return [f"the notification was still queued after {WORK_TIMEOUT:.0f}s — the "
                "message never reached the worker. Check that `broker.current()` "
                "still runs above the first @dramatiq.actor in app/workers/actors.py"]
    if status != "sent":
        return [f"the worker ran but left the notification {status!r}, expected 'sent'"]
    print("  worker       executed deliver_notifications; row is 'sent'")
    return []


def _the_configured_redis_was_the_one_used(redis_url: str) -> list[str]:
    """The assertion that makes the rest of this script mean anything.

    Everything above passes on a misbound build **as long as something is
    listening on localhost:6379** — the enqueuer and the worker misbind together,
    consistently, and the job flows happily through the wrong broker. Verified:
    with `broker.current()` deleted and a Redis on the default port, this script
    printed PASS.

    So do not infer the broker from whether the work happened. Ask the Redis we
    configured whether it saw anything. A correctly bound worker leaves
    `dramatiq:__heartbeats__` here for as long as it lives; a misbound one leaves
    this database completely empty. That holds whatever port CI picks, which
    beats depending on CI to pick a non-default one.
    """
    import redis

    client = redis.Redis.from_url(redis_url)
    try:
        keys = [k.decode() for k in client.keys("dramatiq:*")]
    finally:
        client.close()
    if not keys:
        return [f"nothing dramatiq-shaped in {redis_url} — the work completed, so "
                "the actors are bound to a DIFFERENT broker (probably the "
                "localhost:6379 that `dramatiq.get_broker()` invents). Check that "
                "`broker.current()` runs above the first @dramatiq.actor."]
    print(f"  binding      the configured Redis holds {', '.join(sorted(keys))}")
    return []


def _seed_user(engine) -> int:
    """An adult, active, reachable user. `notify.deliver` suppresses anything
    addressed to a deleted or suspended account, and a suppressed row would let
    this script pass without a message having been delivered at all."""
    with engine.begin() as c:
        return c.scalar(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status, locale)
            VALUES ('+998900000001', 'Smoke', '1995-01-01', 'active', 'uz-Latn')
            RETURNING id
        """))


def _flush_queue(redis_url: str) -> None:
    """Start from an empty queue. A leftover message from a previous run would
    let this pass while the current build's actors are bound to nothing."""
    import redis

    client = redis.Redis.from_url(redis_url)
    client.flushdb()
    client.close()


def _await_boot(worker) -> bool:
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            return False
        time.sleep(POLL)
        # dramatiq logs "Worker process is ready for action" on stderr, which is
        # merged into stdout here — but reading it would block, so boot is
        # detected by the job completing rather than by log scraping. A short
        # settle is enough for the process to connect.
        if time.monotonic() > deadline - BOOT_TIMEOUT + 3.0:
            return True
    return False


def _worker_log(worker) -> str:
    if worker.poll() is None:
        worker.terminate()
    try:
        out = worker.communicate(timeout=15)[0] or ""
    except subprocess.TimeoutExpired:
        worker.kill()
        out = worker.communicate()[0] or ""
    return "\n--- worker log ---\n" + "\n".join(out.splitlines()[-40:])


def _fail(message: str, worker=None) -> int:
    print(f"FAIL  {message}")
    if worker is not None:
        print(_worker_log(worker))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
