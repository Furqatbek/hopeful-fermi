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

Four paths, all required:

  1. **The relay.** An outbox row written in a transaction reaches the queue.
     This is the ~80 lines that ADR-0001 section 6 offers in place of Kafka, so
     "it works" should not be taken on trust.
  2. **The worker.** A message enqueued in this process is executed by a
     `dramatiq` process that was started separately, and its write lands in
     PostgreSQL.
  3. **The scheduler**, which is how the relay actually runs in production —
     `python -m app.workers.scheduler`, one of the two processes the deploy
     story names, and it had never been started by anything. This script called
     `relay.drain()` in-process, which proves the FUNCTION and says nothing about
     the loop around it: `run_forever`, `INTERVALS`, `pass_once`, `unit_of_work`,
     the `__main__` block. Its stop is checked on the EXIT CODE, because SIGTERM
     already terminates a Python process by default — "did it stop" passes with
     the handler deleted, and 0-versus-(-15) is what `docker compose restart`
     actually sees.
  4. **`ingest_audio`**, the heaviest actor there is. `deliver_notifications`
     writes a row; this one downloads a master, shells out to ffmpeg twice,
     encodes, and uploads a delivery file — in a worker process, where the
     dependency is a binary on the image rather than something pytest can fake.
     A missing ffmpeg passes every test in the suite and fails every upload.

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
             "--processes", "1", "--threads", "2",
             # `media` is here for `ingest_audio`. A queue the worker does not
             # consume makes this script hang rather than fail, so the list and
             # the jobs below have to be kept in step.
             "--queues", "notify", "media"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        if not _await_boot(worker):
            return _fail("the worker did not report ready", worker)
        print("  worker       booted against the configured Redis")

        problems = []
        problems += _relay_reaches_the_queue(engine, env)
        problems += _the_scheduler_process_relays(engine, env)
        problems += _worker_executes_a_job(engine, env, user_id, worker)
        problems += _worker_transcodes_audio(engine, env, user_id, worker)
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

    print("PASS  outbox -> relay (in-process AND the scheduler process) -> Redis\n"
          "      -> worker process -> ffmpeg -> PostgreSQL")
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


def _the_scheduler_process_relays(engine, env) -> list[str]:
    """The relay as it is actually deployed: `python -m app.workers.scheduler`.

    Everything above ran `relay.drain()` inside THIS process, which proves the
    function and says nothing about the loop around it — `run_forever`, its
    `INTERVALS` table, `pass_once`, `unit_of_work`, and the `__main__` block, none
    of which anything else executes. One of the two processes the deploy story
    names, and it had never been started.

    (It is NOT a second site for the broker-misbinding failure, which was the
    first thing tried here. `run_forever` calls `broker.configure()`, but
    deleting that line changes nothing: `pass_once` imports `app.workers.actors`,
    whose module-level `broker.current()` configures lazily with the same
    settings. The guard is in `actors.py` and this cannot fail independently of
    it. Established by sabotage, after this docstring claimed otherwise.)

    The stop is checked on the EXIT CODE, not on the process ending. SIGTERM's
    default disposition already terminates a Python process, so "did it stop"
    passes with the handler deleted — verified. A handled stop returns from
    `run_forever` and exits 0; the default kills it with -SIGTERM mid-pass, and
    that is the difference `docker compose restart` sees on every deploy.
    """
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO outbox (event_type, aggregate_type, aggregate_id, payload)
            VALUES ('attempt.started', 'attempt', '2', '{"attempt_id": 2}'::jsonb)
        """))

    scheduler = subprocess.Popen(
        [sys.executable, "-m", "app.workers.scheduler"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + WORK_TIMEOUT
        pending = 1
        while time.monotonic() < deadline:
            if scheduler.poll() is not None:
                return [f"the scheduler exited with code {scheduler.returncode}"]
            with engine.connect() as c:
                pending = c.scalar(text(
                    "SELECT count(*) FROM outbox WHERE dispatched_at IS NULL"))
            if not pending:
                break
            time.sleep(POLL)
        if pending:
            return [f"the scheduler left {pending} outbox row(s) undispatched after "
                    f"{WORK_TIMEOUT:.0f}s — `python -m app.workers.scheduler` is the "
                    "process that relays in production. Check that "
                    "`broker.configure()` still runs before the loop in "
                    "`run_forever`."]

        scheduler.terminate()
        try:
            scheduler.wait(timeout=15)
        except subprocess.TimeoutExpired:
            scheduler.kill()
            return ["the scheduler ignored SIGTERM entirely — every deploy will "
                    "SIGKILL it after the compose timeout. Check the handler in "
                    "`app/workers/scheduler.py`."]
        if scheduler.returncode != 0:
            return [f"the scheduler stopped with {scheduler.returncode} rather than 0 "
                    "— it was killed by the signal instead of handling it, so it "
                    "died mid-pass. `run_forever` should return and exit cleanly."]
    finally:
        if scheduler.poll() is None:
            scheduler.kill()
        scheduler.communicate(timeout=15)
    print("  scheduler    the deployed relay process drained the outbox, "
          "then stopped on SIGTERM")
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


def _worker_transcodes_audio(engine, env, user_id: int, worker) -> list[str]:
    """The heaviest actor there is, and the one whose dependency pytest cannot
    fake.

    `deliver_notifications` writes a row. This one downloads a master from
    storage, shells out to ffmpeg twice — `loudnorm` measurement and then the
    encode — and uploads a delivery file, all inside a worker process where
    ffmpeg is a binary on the image rather than something a fixture can stand in
    for. A worker image built without ffmpeg passes every test in the suite and
    fails every single upload a teacher makes.

    So: a real ten-second sine wave, deliberately quiet, through the real actor
    over the real queue. `status = 'ready'` with a measured `loudness_lufs` is
    the only outcome that means the whole chain ran.
    """
    import shutil
    import tempfile

    if shutil.which("ffmpeg") is None:
        return ["ffmpeg is not on PATH — this is the dependency the media queue "
                "cannot run without, so it is a failure here rather than a skip."]

    from app.platform.storage import storage
    from app.workers.actors import ingest_audio

    with tempfile.TemporaryDirectory() as scratch:
        master = Path(scratch) / "master.wav"
        made = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=220:duration=10:sample_rate=48000",
             "-ac", "2", "-c:a", "pcm_s16le", str(master)],
            capture_output=True, text=True)
        if made.returncode != 0:
            return [f"could not build the master wav: {made.stderr[-300:]}"]
        raw = master.read_bytes()

    key = f"smoke/{uuid.uuid4().hex}/master.wav"
    store = storage()
    store.put(key, raw, content_type="audio/wav")

    with engine.begin() as c:
        asset_id = c.scalar(text("""
            INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                      content_type, bytes, checksum_sha256, status)
            VALUES (:u, 'audio', :b, :k, 'audio/wav', :n, '', 'processing')
            RETURNING id
        """).bindparams(u=user_id, b=store.bucket, k=key, n=len(raw)))

    ingest_audio.send(asset_id)

    deadline = time.monotonic() + WORK_TIMEOUT
    row = None
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            return [f"the worker exited with code {worker.returncode} mid-transcode"]
        with engine.connect() as c:
            row = c.execute(text("""
                SELECT status, loudness_lufs, duration_ms, processing_error
                FROM media_assets WHERE id = :i
            """).bindparams(i=asset_id)).mappings().first()
        if row["status"] != "processing":
            break
        time.sleep(POLL)

    if row["status"] == "processing":
        return [f"the audio asset was still processing after {WORK_TIMEOUT:.0f}s — "
                "the message never reached the media queue. Check that the worker "
                "consumes `media`."]
    if row["status"] != "ready":
        return [f"ingest_audio left the asset {row['status']!r}: "
                f"{row['processing_error']}"]
    if row["loudness_lufs"] is None or row["duration_ms"] is None:
        return ["the asset is ready but carries no measured loudness or duration, "
                "so ffmpeg did not actually run over it"]
    print(f"  worker       executed ingest_audio through ffmpeg; "
          f"{row['duration_ms']} ms at {row['loudness_lufs']} LUFS")
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
