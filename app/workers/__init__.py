"""Background work. Thin actors that call module services and own no domain logic.

Two processes, no more:

    dramatiq app.workers.actors         # the actor pool
    python -m app.workers.scheduler     # the outbox relay and the periodic ticks

The relay is why there is no Kafka (ADR-0001 §6). A domain event is written to
`outbox` in the same transaction as the change that caused it, so an event
without its change — or a change without its event — is not representable. The
relay moves those rows onto Redis, sending BEFORE it marks, which makes delivery
at-least-once and puts the burden of idempotency on the actors, where it belongs.

Import direction: `app.workers` is a composition root, like `app.api`. It may
import any module; no module may import it, and `pyproject.toml`'s import
contracts fail the build if that reverses.
"""
