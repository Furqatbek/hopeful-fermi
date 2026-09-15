"""Dramatiq broker setup.

Redis is the broker; PostgreSQL is the source of truth. That split is the whole
durability story (ADR-0001 §6): a job is enqueued by the RELAY reading a row that
was committed in the same transaction as the domain change, so losing Redis
entirely loses queued work but loses no facts — the relay simply re-dispatches
everything still undispatched.

The consequence for actor authors is one rule, and it is not optional:

    EVERY ACTOR MUST BE IDEMPOTENT.

Delivery is at-least-once by construction. The relay sends, then marks; a crash
between those two points redelivers. That is the correct trade — a duplicated
regrade is a wasted minute of CPU, a lost one is a student with the wrong band.
"""

from __future__ import annotations

import dramatiq
from dramatiq.brokers.stub import StubBroker
from dramatiq.middleware import Middleware

from app.platform.config import settings

_broker: dramatiq.Broker | None = None

# The ids of the processes that booted a worker. `platform.health` reads it and
# names the same key itself, because it cannot import this package.
WORKERS_KEY = "dramatiq:__workers__"


class WorkerPresence(Middleware):
    """Announce this process as a consumer, so health can tell it from a producer.

    The broker's own `__heartbeats__` set cannot. Its Lua script ZADDs the
    CALLING broker's id on every command, enqueues included, so the scheduler —
    which sends a tick every five seconds — heartbeats exactly like a worker,
    and with every worker dead the pool still read alive for as long as the
    scheduler lived. Only a worker process boots a `Worker`, so this hook is the
    one place a consumer can be told apart: its id goes into `WORKERS_KEY`, and
    `health.worker_heartbeat` counts a heartbeat only for an id in that set.
    Freshness still comes from dramatiq's fetch loop; the set only says whose
    heartbeats count.

    A worker that cannot announce itself is left to fail its boot rather than
    run invisibly: unannounced, it would read as a dead pool and page someone
    over a worker that is fine. A graceful stop removes the id. One killed -9
    stays in the set, and that is the reading wanted — its heartbeat ages out
    and the count drops by one, rather than the evidence being pruned.
    """

    def after_worker_boot(self, broker, worker):
        broker.client.sadd(WORKERS_KEY, broker.broker_id)

    def before_worker_shutdown(self, broker, worker):
        try:
            broker.client.srem(WORKERS_KEY, broker.broker_id)
        except Exception:                                  # noqa: BLE001
            # A stale id is harmless (above); a raise here would abort the
            # shutdown sequence the hook is part of.
            pass


class Structlog(Middleware):
    """Bind the message id to every log line the actor emits.

    Without it a worker log is an unattributable stream, and the first question
    anyone asks about a failed job — "which one?" — is unanswerable.
    """

    def before_process_message(self, broker, message):
        import structlog

        structlog.contextvars.bind_contextvars(
            message_id=message.message_id, actor=message.actor_name)

    def after_process_message(self, broker, message, *, result=None, exception=None):
        import structlog

        if exception is not None:
            structlog.get_logger().error("actor_failed", actor=message.actor_name,
                                         error=str(exception))
        structlog.contextvars.clear_contextvars()


def configure(broker: dramatiq.Broker | None = None) -> dramatiq.Broker:
    """Install the broker.

    ORDER MATTERS, and getting it wrong fails silently. `@dramatiq.actor` binds
    whatever `dramatiq.get_broker()` returns AT DECORATION TIME, and
    `get_broker()` helpfully invents a RedisBroker on localhost:6379 if none is
    set. So an actor module imported before this runs is permanently bound to a
    broker pointing at the wrong Redis, and the only symptom is messages that go
    nowhere. `actors.py` therefore calls `current()` at the top of the module,
    above its first decorator.

    Idempotent, so importing `actors` after an explicit `configure()` — which is
    what tests do with a `StubBroker` — does not replace it.
    """
    global _broker
    if broker is None:
        if _broker is not None:
            return _broker
        broker = redis_broker()
    broker.add_middleware(Structlog())
    dramatiq.set_broker(broker)
    _broker = broker
    return broker


def redis_broker() -> dramatiq.Broker:
    """The production broker, over a client with bounded socket timeouts.

    Built with `client=` rather than `url=`, and that is the whole point:
    given a URL, dramatiq builds its own `ConnectionPool` and redis-py ignores
    every socket keyword once a pool is supplied, so a timeout passed that way
    silently does nothing. The bounds matter because this client is called from
    inside `relay.drain`'s `FOR UPDATE` transaction on the scheduler's only
    thread (same reasoning as `platform.realtime.client`): a hung `LPUSH` with
    no timeout would stall the outbox and every periodic tick behind it, and
    never raise into `scheduler._safely` where it would at least be logged.
    With the timeout, a hung broker surfaces as `redis.exceptions.TimeoutError`,
    which `relay._reschedule` treats as transient — backed off, never charged.

    `WorkerPresence` is installed here rather than in `configure`, because it
    writes through `broker.client` and only this broker has one.
    """
    import redis
    from dramatiq.brokers.redis import RedisBroker

    client = redis.Redis.from_url(
        settings().redis_url, socket_timeout=5.0, socket_connect_timeout=2.0,
        retry_on_timeout=False, health_check_interval=30)
    broker = RedisBroker(client=client)
    broker.add_middleware(WorkerPresence())
    return broker


def stub() -> StubBroker:
    """An in-memory broker for tests. Declared here rather than in the test suite
    so the production and test wiring cannot drift.

    Must be called BEFORE `app.workers.actors` is first imported, for the reason
    in `configure`.
    """
    broker = StubBroker()
    broker.emit_after("process_boot")
    return configure(broker)


def current() -> dramatiq.Broker:
    return _broker if _broker is not None else configure()


def reset() -> None:
    """Drop the installed broker. Tests only."""
    global _broker
    _broker = None
