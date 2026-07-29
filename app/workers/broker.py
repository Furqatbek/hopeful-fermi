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
        from dramatiq.brokers.redis import RedisBroker

        broker = RedisBroker(url=settings().redis_url)
    broker.add_middleware(Structlog())
    dramatiq.set_broker(broker)
    _broker = broker
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
