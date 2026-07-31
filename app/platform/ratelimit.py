"""Request budgets, counted in Redis.

The brief asks for "rate limits on content endpoints", and there were none: sixty
consecutive catalogue reads returned sixty 200s, thirty attempt starts thirty
201s. The only limit in the product was hand-rolled in `auth.py` against
`otp_challenges`, and it answered **400**.

Redis rather than a new dependency: it is already deployed for the Dramatiq
broker, already configured (`settings().redis_url`), already in-country with
everything else, and already paid for under the fifty-dollar ceiling. A rate
limiter that needs its own service is a rate limiter that does not ship.

## Fixed window, and why not something better

`INCR` on a key that carries the window index, `EXPIRE` on first write. Two
commands, pipelined, one round trip.

The known cost is a boundary burst: a client can spend its whole budget in the
last second of one window and again in the first second of the next, so the true
worst case over a sliding minute is 2N. A sliding-window log fixes that and costs
a sorted set per client plus a `ZREMRANGEBYSCORE` on every request; GCRA fixes it
and costs a Lua script and a script-reload path. **Neither is worth it here.**
The budgets below are set to catch scraping and runaway clients, where the
difference between 20/min and 40/min in a bad minute changes nothing. Anything
that needs an exact bound — a payment, an OTP that costs real money — is counted
in PostgreSQL where it can be transactional, not here.

## Fail OPEN, deliberately

If Redis is unreachable this allows the request and logs. That is the opposite of
how the entitlement gate fails, and the difference is what each protects.

An entitlement check is a correctness control: getting it wrong gives away the
product. A rate limit is an abuse control: getting it wrong for ninety seconds
during a Redis restart costs a scraper's worth of requests. Failing closed would
put every student in a timed exam on the floor for the same ninety seconds —
mid-paper, clock running, autosave refused. **A limiter that can end an exam is a
worse outage than the abuse it prevents.**

So: allow, and log at WARNING with the reason. Silence would let a limiter that
has been open for a month look exactly like one that is working.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from app.platform.config import settings

log = structlog.get_logger()

_client = None
_broken_since: float | None = None
# How long to stop dialling after a failure. Without this, every request pays the
# connect timeout while Redis is down — the limiter becomes the outage.
_RETRY_AFTER_FAILURE = 30.0


@dataclass(frozen=True, slots=True)
class Budget:
    """`limit` requests per `window_seconds`, per identity."""

    limit: int
    window_seconds: int = 60

    def __post_init__(self) -> None:
        if self.limit < 1 or self.window_seconds < 1:
            raise ValueError("a budget must allow at least one request per window")


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    remaining: int
    retry_after: int


def client():
    """One connection pool per process, built lazily.

    Lazily because importing this module must not require Redis: `make test-unit`
    runs the whole non-integration suite with no services at all, and the
    application imports this at startup.
    """
    global _client
    if _client is None:
        import redis

        _client = redis.Redis.from_url(
            settings().redis_url,
            socket_timeout=0.25, socket_connect_timeout=0.25,
            retry_on_timeout=False, health_check_interval=30)
    return _client


def reset() -> None:
    """Drop the cached client and the failure latch.

    For tests that repoint `REDIS_URL`, and for the same reason
    `platform.db.reset_engine` exists: a cached connection outliving its
    configuration is a process talking to the wrong server.
    """
    global _client, _broken_since
    _client = None
    _broken_since = None


def check(key: str, budget: Budget, *, now: float | None = None) -> Verdict:
    """Spend one unit of `key`'s budget.

    Called once per request, so it must never raise and never block for long —
    hence the 250 ms socket timeout and the latch. `key` is the caller's business:
    this module has no opinion about users, routes or IP addresses, which is what
    keeps it in `platform`.
    """
    global _broken_since
    now = time.time() if now is None else now

    if _broken_since is not None and now - _broken_since < _RETRY_AFTER_FAILURE:
        return Verdict(True, budget.limit, 0)

    window = int(now // budget.window_seconds)
    resets_in = int((window + 1) * budget.window_seconds - now) or 1
    try:
        pipe = client().pipeline()
        pipe.incr(f"rl:{key}:{window}")
        pipe.expire(f"rl:{key}:{window}", budget.window_seconds + 1)
        used = int(pipe.execute()[0])
    except Exception as exc:                       # noqa: BLE001 — see the module docstring
        if _broken_since is None:
            log.warning("ratelimit_open", reason=str(exc), key=key)
        _broken_since = now
        return Verdict(True, budget.limit, 0)

    _broken_since = None
    if used > budget.limit:
        return Verdict(False, 0, resets_in)
    return Verdict(True, budget.limit - used, resets_in)
