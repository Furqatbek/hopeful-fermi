"""The realtime transport kernel: handshake tickets, and the fan-out bus.

Redis, and specifically Redis PUB/SUB, for the same reason `ratelimit.py` gives:
it is already deployed for the Dramatiq broker, already configured, already
in-country, already inside the fifty-dollar ceiling. Four gunicorn workers means
a `slot.matched` produced while a worker drains the outbox has to reach a socket
held by a different process, and that is a message broker whether or not it is
called one. The alternative — a second broker, or sticky sessions plus a
publisher that knows which worker holds which connection — is the "not yet"
answer from ADR-0001 §4.

## Failing CLOSED, which is the opposite of the rate limiter

`ratelimit.py` fails OPEN and says why: a limiter that can end an exam is worse
than the abuse it prevents. This module fails CLOSED, and the difference is that
there is no useful degraded mode to fall into.

  * **Tickets.** A ticket that cannot be stored cannot be verified, and a gateway
    that admits an unverifiable ticket is an authentication bypass on a socket
    that then subscribes to channels. `mint` raises; `redeem` returns nothing.
  * **Delivery.** With the bus down there is no way to deliver a frame. A
    connection that stays open and silently receives nothing is precisely the
    *partial view* the contract's explicit `resync` exists to make impossible —
    a student watching a leaderboard that quietly stopped. So the gateway closes
    the socket and the client reconnects with backoff.

The cost of failing closed here is bounded and was designed for: §4.7 of the API
contract puts exam answers, the exam clock and every other correctness-critical
path on HTTP precisely so that losing the socket costs nothing but freshness.
The one thing that genuinely degrades is live speaking, which cannot work
without a signalling path anyway.

## Ordering, sequence numbers and the replay buffer

Every channel carries a monotonic `seq` from `INCR`, and the last ~100 frames are
kept in a capped list for ~5 minutes. A client that drops mid-competition
resumes with the last seq it saw; if that point has fallen out of the buffer the
gateway sends `resync` rather than continuing on a hole. On these networks a
tunnel drop mid-contest is routine, not exceptional.

`INCR` is atomic across workers, so two processes publishing to one channel
cannot mint the same seq. The frame is written to the log BEFORE it is published,
so a subscriber that receives seq N can always find N-1 in the buffer; the other
order would let a live frame arrive before its own replay entry existed.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog

from app.platform.config import settings

log = structlog.get_logger()

#: 30 s, as the contract says, and the number is not arbitrary. A ticket is a
#: bearer credential in a URL: it lands in proxy access logs, in browser history,
#: in `Referer` on any page the socket page links to, and in whatever the
#: corporate middlebox at a centre keeps. Thirty seconds is one round trip more
#: than a handshake needs — the client already holds the response — and short
#: enough that a ticket harvested from a log is dead long before anyone reads the
#: log. Single-use on top, so even a ticket stolen in flight is a race the
#: legitimate client normally wins.
TICKET_TTL_SECONDS = 30

#: How much history a channel keeps, and for how long. 100 frames is ~4 minutes
#: of `leaderboard.delta` at its 3-second throttle — one tunnel outage. Longer
#: buys little: past a few minutes the client is better off refetching state over
#: HTTP, which is exactly what `resync` tells it to do.
REPLAY_FRAMES = 100
REPLAY_TTL_SECONDS = 300

#: Longer than the log, so a resume inside the replay window can never see the
#: counter restart at 1 and read a fresh frame as a stale one.
SEQ_TTL_SECONDS = 86_400

_TICKET = "rt:ticket:"
_TOPIC = "rt:ch:"
_LOG = "rt:log:"
_SEQ = "rt:seq:"

_sync_client = None


class BusUnavailable(RuntimeError):
    """Redis did not answer. Raised rather than swallowed — see the module
    docstring on why this module fails closed."""


@dataclass(frozen=True, slots=True)
class Ticket:
    token: str
    user_xid: str
    expires_at: dt.datetime


def client():
    """The synchronous client, for HTTP handlers and worker processes.

    Built lazily and cached per process, same as `ratelimit.client`, and for the
    same reason: importing this module must not require Redis, because
    `make test-unit` runs the whole non-integration suite with no services.

    A tighter timeout than the limiter's 250 ms would be wrong here — this is not
    on every request — but it is still bounded, because a hung `publish` inside
    the relay's transaction would stall the outbox for everything.
    """
    global _sync_client
    if _sync_client is None:
        import redis

        _sync_client = redis.Redis.from_url(
            settings().redis_url, socket_timeout=1.0, socket_connect_timeout=1.0,
            retry_on_timeout=False, health_check_interval=30,
            decode_responses=True)
    return _sync_client


def reset() -> None:
    """Drop the cached client. For tests that repoint `REDIS_URL`, exactly as
    `ratelimit.reset` exists for."""
    global _sync_client
    _sync_client = None


def async_client():
    """A fresh async client. Deliberately NOT cached.

    Each connection gets its own, because each connection gets its own pub/sub
    subscription set. That costs one Redis connection per socket — ~200 at the
    peak this system is sized for, against a `maxclients` of 10,000 — and buys a
    property no amount of care buys back: **one socket cannot receive another
    socket's frames, because it is not physically subscribed to them.** A shared
    per-process demultiplexer with a refcounted channel table would halve the
    connection count and put the org-private promise behind a refcounting bug.
    Revisit around 2,000 concurrent, where the arithmetic changes.
    """
    import redis.asyncio

    return redis.asyncio.Redis.from_url(
        settings().redis_url, socket_timeout=5.0, socket_connect_timeout=2.0,
        health_check_interval=30, decode_responses=True)


# ── tickets ──────────────────────────────────────────────────────────

def mint(user_xid: str, *, now: dt.datetime | None = None) -> Ticket:
    """Issue a single-use handshake ticket bound to this user.

    `NX` so a token collision — astronomically unlikely with 256 bits, but the
    failure mode is one user's socket authenticated as another — cannot silently
    overwrite an existing binding.
    """
    moment = now or dt.datetime.now(dt.UTC)
    token = secrets.token_urlsafe(32)
    try:
        stored = client().set(_TICKET + token, user_xid,
                              ex=TICKET_TTL_SECONDS, nx=True)
    except Exception as exc:                       # noqa: BLE001 — see the docstring
        raise BusUnavailable(str(exc)) from exc
    if not stored:
        # 256 bits colliding inside 30 seconds does not happen; refusing is
        # still the only safe answer, because `SET` without `NX` would rebind a
        # live ticket to a different user — one person's socket authenticated as
        # another. No `# pragma: no cover` over it: a pragma does not say a line
        # is safe, it says the coverage gate must not look at it, and this
        # project has already been bitten by one sitting over a safety check
        # (`speaking/service._create_pair`). The test forces the collision by
        # fixing the token generator.
        raise BusUnavailable("ticket collision")
    return Ticket(token, user_xid,
                  moment + dt.timedelta(seconds=TICKET_TTL_SECONDS))


async def redeem(token: str, redis_client) -> str | None:
    """Exchange a ticket for the user xid it was issued to, and burn it.

    `GETDEL`, which is one atomic command. Read-then-delete would let two
    handshakes racing on a stolen ticket both succeed — the whole point of
    "single use" is that the second one loses.

    Returns `None` for expired, unknown, already-burned and Redis-unreachable
    alike: the gateway must not tell an unauthenticated caller which it was.
    """
    if not token or len(token) > 128:
        return None
    try:
        return await redis_client.getdel(_TICKET + token)
    except Exception as exc:                       # noqa: BLE001 — see the docstring
        log.warning("realtime_ticket_unverifiable", error=str(exc)[:200])
        return None


# ── frames ───────────────────────────────────────────────────────────

def frame(channel: str, event_type: str, data: dict, seq: int,
          *, now: dt.datetime | None = None) -> dict:
    """The `RtEnvelope` from the contract, and the only shape on the wire."""
    moment = now or dt.datetime.now(dt.UTC)
    return {"type": event_type, "channel": channel, "seq": seq,
            "ts": moment.isoformat().replace("+00:00", "Z"), "data": data}


def publish(channel: str, event_type: str, data: dict,
            *, now: dt.datetime | None = None) -> int:
    """Allocate a seq, append to the replay log, fan out. Returns the seq.

    Called from worker processes and from HTTP handlers, both synchronous. One
    pipeline, one round trip: at the relay's one-second cadence a second round
    trip per event is a second of outbox lag under a competition burst.
    """
    try:
        connection = client()
        seq = int(connection.incr(_SEQ + channel))
        connection.expire(_SEQ + channel, SEQ_TTL_SECONDS)
        body = json.dumps(frame(channel, event_type, data, seq, now=now))
        pipe = connection.pipeline()
        # Logged before it is published, so a subscriber that sees seq N can
        # always find N-1 in the buffer. The other order has a window where a
        # live frame outruns its own replay entry.
        pipe.lpush(_LOG + channel, body)
        pipe.ltrim(_LOG + channel, 0, REPLAY_FRAMES - 1)
        pipe.expire(_LOG + channel, REPLAY_TTL_SECONDS)
        pipe.publish(_TOPIC + channel, body)
        pipe.execute()
    except BusUnavailable:                                        # pragma: no cover
        raise
    except Exception as exc:                       # noqa: BLE001 — see the docstring
        raise BusUnavailable(str(exc)) from exc
    return seq


async def replay(channel: str, since_seq: int, redis_client) -> tuple[list[dict], bool]:
    """What this subscriber missed, and whether the answer is complete.

    Returns `(frames, complete)`. `complete` False means the requested point has
    fallen out of the buffer and the client must refetch over HTTP — the gateway
    turns that into `RtResync`. Saying so explicitly is the whole design: a
    client that silently resumes on a hole shows a board that stopped updating
    and looks correct while doing it.
    """
    raw = await redis_client.lrange(_LOG + channel, 0, -1)
    frames = [json.loads(item) for item in reversed(raw)]
    missed = [item for item in frames if item.get("seq", 0) > since_seq]

    if not frames:
        # Nothing buffered. Either the channel has been silent (nothing missed)
        # or the whole buffer aged out (everything missed). The counter, which
        # outlives the log, is what tells the two apart.
        current = await redis_client.get(_SEQ + channel)
        return [], int(current or 0) <= since_seq

    oldest = frames[0].get("seq", 0)
    return missed, oldest <= since_seq + 1


@asynccontextmanager
async def subscription(redis_client) -> AsyncIterator["Subscription"]:
    """A pub/sub handle scoped to one connection, closed on the way out.

    A context manager because the failure this prevents is the expensive one: a
    socket that dies without releasing its Redis connection leaks a file
    descriptor per dropped client, and dropped clients are the normal case on
    these networks.
    """
    import anyio

    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    try:
        yield Subscription(pubsub)
    finally:
        # Shielded and bounded, for the reason `api/realtime.serve` documents at
        # length: this runs while the task is being cancelled, and an unshielded
        # await here is cancelled before the UNSUBSCRIBE is sent — leaving the
        # server-side subscription in place for a client that has gone.
        with anyio.move_on_after(2.0, shield=True):
            await pubsub.aclose()


class Subscription:
    """The channels one connection is listening to.

    Deliberately thin. Ownership of the poll loop belongs to the caller — the
    gateway runs it as one task so that subscribe, unsubscribe and receive are
    serialised on a single object with no lock, which is the simplest way to be
    certain a `subscribe` cannot interleave with a `get_message` mid-protocol.
    """

    def __init__(self, pubsub) -> None:
        self._pubsub = pubsub
        self._channels: set[str] = set()

    async def add(self, channel: str) -> None:
        if channel in self._channels:
            return
        await self._pubsub.subscribe(_TOPIC + channel)
        self._channels.add(channel)

    async def drop(self, channel: str) -> None:
        if channel not in self._channels:
            return
        await self._pubsub.unsubscribe(_TOPIC + channel)
        self._channels.discard(channel)

    async def next_frame(self, *, timeout: float) -> dict | None:
        """One frame, or `None` when the poll window expired with nothing on it.

        A timeout rather than an endless `listen()` so the same task can service
        subscribe requests and notice the connection has gone.
        """
        message = await self._pubsub.get_message(timeout=timeout)
        if not message or message.get("type") != "message":
            return None
        return json.loads(message["data"])
