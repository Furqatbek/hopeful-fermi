"""The WebSocket gateway. The one thing in this application that is a server.

`docs/design/0003-api-contract.md` §4 has described this protocol since before
there was any code behind it, which is why speaking check-in has been telling
clients to "await `slot.matched` on the realtime channel" while nothing in the
process could ever send one. This is that channel.

`async def`, which with `GET /media/{xid}/content` is the entire exception
ADR-0001 §5.7 carves out of "sync handlers by default". A sync handler occupies
one of the worker's forty threadpool slots for its duration; two hundred sockets
held open for an hour is not a shape a threadpool has. The corollary is the rule
this module lives under: **nothing here may block the event loop.** One blocking
`session.execute` inside a coroutine stalls every other connection on that
worker, and the symptom — everyone's frames arriving in bursts — looks like a
network problem rather than a code one. So every database read is pushed to a
thread with `anyio.to_thread.run_sync`, and it opens and closes its own
transaction inside that thread. A socket that has been idle for an hour holds no
database connection at all.

## Shape of a connection

Three tasks, and each exists to stop one thing:

  * **pump** owns the Redis pub/sub handle. Subscribe, unsubscribe and receive
    are serialised on one task, so no lock is needed and no `subscribe` can
    interleave with a `get_message` mid-protocol.
  * **reader** reads client frames. It is the only task that touches the socket's
    receive side.
  * **writer** is the only task that touches the send side, drains a BOUNDED
    queue, and emits the heartbeat.

Backpressure is the queue's `maxsize`. When it is full the connection is closed,
deliberately: the alternative is an unbounded buffer, which is a memory leak with
a slow phone attached to it. Sixty-four frames is roughly three minutes of
`leaderboard.delta` — a client that far behind is not going to catch up, and
telling it to reconnect and resync is both cheaper and more correct than feeding
it three-minute-old ranks.

## Under the deployed topology

`docker-compose.yml` runs four gunicorn workers with `UvicornWorker`, plus a
scheduler process and two actor processes. Three consequences, all checked:

  * A connection lands on whichever worker Caddy picked, and the process that
    produces its events is usually a different one — which is why the fan-out is
    Redis PUB/SUB and not an in-process registry.
    `test_a_frame_published_by_another_process_arrives` publishes from a separate
    interpreter, because a same-process test would pass against a dictionary.
  * Two hundred concurrent sockets is ~50 per worker, each holding its own Redis
    connection (see `platform.realtime.async_client` for why they are not
    shared). Four hundred Redis connections at the very top against a default
    `maxclients` of 10,000.
  * PostgreSQL is the constraint the compose file sizes everything against —
    ten connections per process, seventy of a hundred in total. This gateway
    holds none between frames: the only database work is the handshake and each
    subscribe, both of which open and close a transaction inside a worker
    thread. It does draw on the same forty-slot anyio threadpool the sync HTTP
    handlers use, so a reconnect storm competes with requests for a moment —
    acceptable because the work is one indexed `SELECT`, and the reason the
    authorization for a twenty-channel subscribe is one thread hop and one
    transaction rather than twenty.

## What this gateway is NOT allowed to do

**Parse signalling.** `signal.*` frames carry SDP and ICE and are relayed
byte-for-byte. The server is a mailbox, not a media component; audio never
transits these servers and the moment this code understood an SDP body that
would stop being structurally true.

**Decide who may read a channel.** That is `app/modules/authz/channels.py`,
beside the HTTP policy engine, for the reason stated there.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import time
from typing import Any

import anyio
import structlog
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.api.deps import Principal, resolve_principal
from app.modules.authz import channels
from app.platform import realtime
from app.platform.db import unit_of_work

log = structlog.get_logger()

#: Server ping cadence, and how many may be missed. Both from §4.6. Module
#: constants rather than settings because they are protocol, not deployment —
#: `hello` reports the value actually in force, so a test that shortens them is
#: testing the same code path the product runs.
HEARTBEAT_SECONDS = 25.0
MISSED_HEARTBEATS_BEFORE_CLOSE = 2

#: §4.4's implicit limit. Twenty is more channels than any real client wants —
#: a student watches one contest, one attempt and their own — and the cap is
#: what stops one socket subscribing to every competition on the platform and
#: turning a refused read into a slow enumeration.
MAX_CHANNELS = 20

#: Frames the connection may hold for a client that has stopped reading.
SEND_QUEUE = 64
#: How long one `send` may take before the client is declared gone. Generous for
#: a 3G phone, finite so a half-open TCP connection cannot pin the writer task.
SEND_TIMEOUT_SECONDS = 10.0

#: Inbound frame budget, per connection, counted in this process. Deliberately
#: not `platform.ratelimit`: that counts in Redis with a blocking client, and a
#: per-connection limit needs no shared state to be correct. Set well above an
#: ICE gathering burst (~40 candidates in a few seconds) so only abuse reaches it.
INBOUND_FRAMES = 240
INBOUND_WINDOW_SECONDS = 10.0

#: A signalling frame is a few kilobytes of SDP. The cap is on the ENCODED frame
#: and exists because every byte accepted here is republished through Redis to
#: the peer — an unbounded relay is a memory amplifier with two endpoints.
MAX_FRAME_BYTES = 64 * 1024

#: WebSocket close codes. 1008 is "policy violation", 1013 "try again later".
CLOSE_UNAUTHORIZED = 4401
CLOSE_TOO_SLOW = 1013
CLOSE_POLICY = 1008
CLOSE_BUS_DOWN = 1011

#: Where a client refetches state after a `resync`, per channel family. Only the
#: families whose state has an HTTP mirror appear: `pair:` has none — a dropped
#: call is re-established by the peers, not reloaded — and inventing a URL that
#: answers 404 would be worse than omitting the field the schema makes optional.
REFETCH = {
    "competition": "/api/v1/competitions/{xid}/leaderboard",
    "attempt": "/api/v1/attempts/{xid}",
    "assignment": "/api/v1/assignments/{xid}/progress",
    "slot": "/api/v1/speaking/slots",
    "queue": "/api/v1/speaking/queue",
    "safety": "/api/v1/admin/reports",
}

_CLIENT_FRAMES = frozenset({"subscribe", "unsubscribe", "signal", "pong", "ping"})
_SIGNAL_KINDS = frozenset({"offer", "answer", "ice", "bye"})


def install(app) -> None:
    """Mount the gateway.

    At `/realtime`, not under `/api/v1`: that is the URL the contract publishes
    and `/realtime/ticket` hands out. It is a WebSocket route, so it cannot
    appear in the generated OpenAPI document as an HTTP path — FastAPI's
    generator walks `APIRoute` only — which is what keeps
    `scripts/check_api_coverage.py` at parity instead of failing on an undeclared
    path. `tests/integration/test_realtime.py` asserts that rather than trusting
    it, because it is a property of a framework internal.
    """

    @app.websocket("/realtime")
    async def realtime_gateway(websocket: WebSocket, ticket: str = "") -> None:
        await serve(websocket, ticket)


async def serve(websocket: WebSocket, ticket: str) -> None:
    """One connection, start to finish."""
    redis_client = realtime.async_client()
    try:
        user_xid = await realtime.redeem(ticket, redis_client)
        if user_xid is None:
            # Accept, then close with a code. A handshake rejected outright gives
            # a browser an opaque "connection failed" with no way to tell an
            # expired ticket from a dead server, and the client's correct
            # response to those differs: re-mint versus back off.
            await websocket.accept()
            await _close(websocket, CLOSE_UNAUTHORIZED, "ticket_invalid")
            return

        actor = await anyio.to_thread.run_sync(_load_principal, user_xid)
        if actor is None:
            await websocket.accept()
            await _close(websocket, CLOSE_UNAUTHORIZED, "account_inactive")
            return

        await websocket.accept()
        await _run(websocket, redis_client, actor)
    finally:
        # Shielded, and bounded. A client going away cancels this task, and an
        # unshielded `aclose()` is cancelled at its first await — which leaks the
        # Redis connection this socket was using. Dropped clients are the NORMAL
        # case on these networks, so that is one leaked connection per dropped
        # phone until the process is restarted. Proven, not guessed: without the
        # shield, `test_a_resume_inside_the_buffer_replays_only_what_was_missed`
        # fails on teardown with `CancelledError` raised from inside
        # `ConnectionPool.disconnect`.
        #
        # `move_on_after` rather than a bare shield, because a shield with no
        # deadline turns a hung Redis into a hung worker task.
        with anyio.move_on_after(2.0, shield=True):
            await redis_client.aclose()


def _load_principal(user_xid: str) -> Principal | None:
    """Resolved through `deps.resolve_principal`, the same function the HTTP side
    uses — so roles, memberships and the active-account check cannot differ
    between the two transports.

    Re-read at handshake rather than carried on the ticket, which is what makes a
    ban that landed in the last thirty seconds take effect: the ticket says who
    you are, the database says what you may do.
    """
    with unit_of_work() as session:
        try:
            return resolve_principal(session, user_xid)
        except Exception:                              # noqa: BLE001
            # `resolve_principal` raises Forbidden for a suspended or deleted
            # account. There is no problem+json on a socket, so it becomes a
            # close code.
            return None


async def _run(websocket: WebSocket, redis_client, actor: Principal) -> None:
    outbound: asyncio.Queue[dict] = asyncio.Queue(maxsize=SEND_QUEUE)
    commands: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    state = _Connection(actor=actor, outbound=outbound, commands=commands)

    # Subscribed before the first frame is read. §4.4 calls `user:{me}` implicit,
    # and it has to be: `session.revoked` and `slot.matched` are the two frames a
    # client most needs and the two it would be racing to subscribe to. A student
    # who checked in and then reconnected would otherwise miss the pairing that
    # happened in the half second before their `subscribe` arrived.
    implicit = channels.describe(actor)["implicit_channels"]
    state.subscribed.update(implicit)
    commands.put_nowait(("subscribe", (implicit, None, 0)))

    await outbound.put({
        "type": "hello",
        "data": {"server_now": _now_iso(),
                 "heartbeat_seconds": int(HEARTBEAT_SECONDS),
                 "max_channels": MAX_CHANNELS,
                 "implicit_channels": implicit},
    })

    async with realtime.subscription(redis_client) as feed:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_pump, feed, state, redis_client, tasks.cancel_scope)
            tasks.start_soon(_writer, websocket, state, tasks.cancel_scope)
            tasks.start_soon(_reader, websocket, state, tasks.cancel_scope)
    if state.close_code is not None:
        await _close(websocket, state.close_code, state.close_reason)


class _Connection:
    """Everything one connection knows about itself.

    Mutable, and shared by the three tasks. `subscribed` is written by the reader
    and read by the pump on every frame, which is why an unsubscribe takes effect
    the instant the reader sees it rather than when Redis catches up.
    """

    def __init__(self, *, actor: Principal, outbound: asyncio.Queue,
                 commands: asyncio.Queue, now: float | None = None) -> None:
        started = time.monotonic() if now is None else now
        self.actor = actor
        self.outbound = outbound
        self.commands = commands
        self.subscribed: set[str] = set()
        self.last_seen = started
        self.close_code: int | None = None
        self.close_reason = ""
        self._window_started = started
        self._frames_in_window = 0

    def offer(self, frame: dict) -> bool:
        """Enqueue for the writer. False when the client is too far behind.

        `put_nowait`, never `await put`. Awaiting a full queue would block the
        PUMP, and the pump is what drains this connection's Redis subscription —
        so one dead phone would stop its own frames being read out of Redis and
        grow a server-side output buffer there instead. Refusing here moves the
        decision to the one place that can act on it: drop the connection.
        """
        try:
            self.outbound.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            return False

    def within_budget(self, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        if moment - self._window_started >= INBOUND_WINDOW_SECONDS:
            self._window_started = moment
            self._frames_in_window = 0
        self._frames_in_window += 1
        return self._frames_in_window <= INBOUND_FRAMES

    def stop(self, code: int, reason: str) -> None:
        if self.close_code is None:
            self.close_code = code
            self.close_reason = reason


# ── the three tasks ──────────────────────────────────────────────────

async def _pump(feed, state: _Connection, redis_client, cancel_scope) -> None:
    """Redis -> the outbound queue, and the only task that touches `feed`."""
    while True:
        while not state.commands.empty():
            action, payload = state.commands.get_nowait()
            if action == "subscribe":
                await _add(feed, state, redis_client, *payload)
            else:
                await _remove(feed, state, *payload)

        try:
            frame = await feed.next_frame(timeout=0.2)
        except Exception as exc:                       # noqa: BLE001
            # The bus went away. Closing rather than continuing: see
            # `platform/realtime.py` on why a silently empty socket is the one
            # outcome the protocol's explicit resync exists to prevent.
            #
            # A close code and a reason, not an `RtError`: the contract's error
            # enum has no member for "the service is down", and picking the
            # nearest one would tell the client something untrue about its
            # credentials. 1011 with `bus_unavailable` is unambiguous.
            log.warning("realtime_bus_lost", error=str(exc)[:200])
            state.stop(CLOSE_BUS_DOWN, "bus_unavailable")
            cancel_scope.cancel()
            return
        if frame is None:
            continue
        # `_unsubscribe` removes the channel from `subscribed` on the READER
        # task and leaves the Redis UNSUBSCRIBE to be issued here, one queue
        # later. Anything arriving in between is a frame for a channel this
        # connection has already dropped, and this line is the only thing that
        # refuses it — `test_a_frame_for_a_dropped_channel_is_not_delivered`
        # deletes it and watches the frame reach the outbound queue.
        if frame.get("channel") not in state.subscribed:
            continue
        if not state.offer(frame):
            # Cancelled rather than drained: the queue is full by definition, so
            # there is no way to tell this client anything, and every further
            # frame would be another one to throw away.
            state.stop(CLOSE_TOO_SLOW, "send_queue_full")
            cancel_scope.cancel()
            return


async def _writer(websocket: WebSocket, state: _Connection, cancel_scope) -> None:
    """The outbound queue -> the socket, plus the heartbeat and the liveness reaper."""
    try:
        while True:
            try:
                frame = await asyncio.wait_for(state.outbound.get(),
                                               timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                silent = time.monotonic() - state.last_seen
                if silent > HEARTBEAT_SECONDS * (MISSED_HEARTBEATS_BEFORE_CLOSE + 0.5):
                    # Two missed heartbeats. A half-open TCP connection is
                    # indistinguishable from a quiet one at the socket layer and
                    # will otherwise hold this connection's memory until the
                    # kernel gives up, which is hours.
                    state.stop(CLOSE_POLICY, "heartbeat_timeout")
                    break
                frame = {"type": "ping", "data": {"server_now": _now_iso()}}

            await asyncio.wait_for(websocket.send_text(json.dumps(frame)),
                                   timeout=SEND_TIMEOUT_SECONDS)
            if state.close_code is not None:
                # A refusal or a too-slow verdict still gets its frame on the
                # wire before the socket goes, so the client can log something
                # more useful than a close code.
                break
    except (TimeoutError, WebSocketDisconnect, RuntimeError):
        state.stop(CLOSE_TOO_SLOW, "send_timeout")
    finally:
        cancel_scope.cancel()


async def _reader(websocket: WebSocket, state: _Connection, cancel_scope) -> None:
    """The socket -> command handling. The only task that receives."""
    try:
        while True:
            raw = await websocket.receive_text()
            state.last_seen = time.monotonic()
            if len(raw) > MAX_FRAME_BYTES:
                _refuse(state, "bad_frame", "That frame is too large.")
                continue
            if not state.within_budget():
                _refuse(state, "rate_limited", "Too many frames. Slow down.")
                state.stop(CLOSE_POLICY, "inbound_flood")
                break
            await _handle(state, raw)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        cancel_scope.cancel()


# ── client frames ────────────────────────────────────────────────────

async def _handle(state: _Connection, raw: str) -> None:
    try:
        message = json.loads(raw)
    except ValueError:
        _refuse(state, "bad_frame", "That frame is not JSON.")
        return
    if not isinstance(message, dict):
        _refuse(state, "bad_frame", "A frame must be an object.")
        return

    kind = message.get("type")
    if kind not in _CLIENT_FRAMES:
        _refuse(state, "bad_frame", "Unknown frame type.", request_id=message.get("id"))
        return
    if kind in ("ping", "pong"):
        # `last_seen` is already stamped by the reader; a pong needs nothing else.
        return
    if kind == "subscribe":
        await _subscribe(state, message)
    elif kind == "unsubscribe":
        await _unsubscribe(state, message)
    else:
        _signal(state, message)


async def _subscribe(state: _Connection, message: dict) -> None:
    names = message.get("channels")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        _refuse(state, "bad_frame", "`channels` must be a list of strings.",
                request_id=message.get("id"))
        return
    since = message.get("since_seq")
    since_seq = since if isinstance(since, int) and since >= 0 else 0

    held = {str(parsed) for parsed in map(channels.parse, names)
            if parsed is not None and str(parsed) in state.subscribed}
    wanted = [n for n in names if str(channels.parse(n) or n) not in held]
    if len(state.subscribed) + len(wanted) > MAX_CHANNELS:
        _refuse(state, "forbidden_channel",
                f"A connection may hold at most {MAX_CHANNELS} channels.",
                request_id=message.get("id"))
        return

    # ONE thread hop and ONE transaction for the whole batch, closed before this
    # returns. A per-channel session would be twenty round trips and twenty
    # connections checked out for a client that subscribes to twenty channels at
    # once, which is exactly what a competition client does.
    verdicts = await anyio.to_thread.run_sync(_authorize, state.actor, tuple(wanted))

    accepted: list[str] = sorted(held)
    for name, (channel, verdict) in zip(wanted, verdicts, strict=True):
        if not verdict.allowed:
            log.info("realtime_channel_refused", user=state.actor.user_xid,
                     channel=name, reason=verdict.reason)
            _refuse(state, verdict.code, "You may not subscribe to that channel.",
                    channel=name, request_id=message.get("id"))
            continue
        canonical = str(channel)
        state.subscribed.add(canonical)
        accepted.append(canonical)

    # The acknowledgement is emitted by the PUMP, once Redis has actually
    # subscribed — not here. A `subscribed` frame sent before the SUBSCRIBE
    # command has been issued is a promise the connection cannot yet keep: a
    # client that reads it and then acts (checks in, registers, starts an
    # attempt) can race the event it was waiting for and never see it.
    state.commands.put_nowait(("subscribe", (accepted, message.get("id"), since_seq)))


def _authorize(actor: Principal,
               names: tuple[str, ...]) -> list[tuple[channels.Channel | None,
                                                     channels.Verdict]]:
    """Runs on a worker thread. Opens one transaction and closes it here.

    The whole reason this is a separate function: the session must not outlive
    the call. A connection that lives for an hour holding a pooled PostgreSQL
    connection would exhaust `pool_size=5, max_overflow=5` after ten sockets.
    """
    if not names:
        return []
    with unit_of_work() as session:
        return [channels.decide(actor, name, session) for name in names]


async def _unsubscribe(state: _Connection, message: dict) -> None:
    names = message.get("channels")
    if not isinstance(names, list):
        _refuse(state, "bad_frame", "`channels` must be a list of strings.",
                request_id=message.get("id"))
        return
    dropped = []
    for name in names:
        parsed = channels.parse(name) if isinstance(name, str) else None
        canonical = str(parsed) if parsed else name
        if canonical in state.subscribed:
            state.subscribed.discard(canonical)
            dropped.append(canonical)
    state.commands.put_nowait(("unsubscribe", (dropped, message.get("id"))))


def _signal(state: _Connection, message: dict) -> None:
    """Relay a WebRTC signalling frame to the peer, without reading it.

    Three things are checked, and none of them is the payload:

      1. the channel is one this connection is already subscribed to — which is
         how the authorization above covers signalling too, rather than a second
         check that could disagree with it;
      2. `kind` is one of the four in the contract, because the event type
         (`signal.offer`) is what `channels.carries` matches on;
      3. the frame fits.

    `payload` is passed through untouched. It is SDP or an ICE candidate, it is
    the peers' business, and a server that parsed it would be a media component —
    which is the line ADR-0001 §7 draws to keep audio off these servers.
    `from` is added ALONGSIDE it so a peer can tell the other side's frame from
    the echo of its own; that is envelope, not content.
    """
    channel = message.get("channel")
    data = message.get("data")
    if not isinstance(channel, str) or channel not in state.subscribed:
        _refuse(state, "forbidden_channel", "You are not subscribed to that channel.",
                channel=channel if isinstance(channel, str) else None,
                request_id=message.get("id"))
        return
    if not isinstance(data, dict) or data.get("kind") not in _SIGNAL_KINDS:
        _refuse(state, "bad_frame", "A signal needs a kind of offer, answer, ice or bye.",
                channel=channel, request_id=message.get("id"))
        return

    event = f"signal.{data['kind']}"
    if not channels.carries(channel.partition(":")[0], event):
        _refuse(state, "forbidden_channel", "That channel does not carry signalling.",
                channel=channel, request_id=message.get("id"))
        return
    try:
        realtime.publish(channel, event,
                         {"kind": data["kind"], "payload": data.get("payload"),
                          "from": state.actor.user_xid})
    except realtime.BusUnavailable:
        state.stop(CLOSE_BUS_DOWN, "bus_unavailable")


# ── helpers ──────────────────────────────────────────────────────────

async def _add(feed, state: _Connection, redis_client, names: list[str],
               request_id: Any, since_seq: int) -> None:
    """Issue the Redis SUBSCRIBE, replay, then acknowledge — in that order.

    The order is the point. Replaying before the subscription is live leaves a
    gap between the last replayed frame and the first live one; acknowledging
    before either lets a client act on a subscription that does not exist yet.
    """
    for name in names:
        await feed.add(name)
    if since_seq:
        for name in names:
            await _replay_into(state, redis_client, name, since_seq)
    state.offer({"type": "subscribed", "id": request_id,
                 "data": {"channels": names}})


async def _remove(feed, state: _Connection, names: list[str],
                  request_id: Any) -> None:
    for name in names:
        await feed.drop(name)
    state.offer({"type": "unsubscribed", "id": request_id,
                 "data": {"channels": names}})


async def _replay_into(state: _Connection, redis_client,
                       channel: str, since_seq: int) -> None:
    """Deliver what a resuming client missed, or tell it to refetch.

    Runs on the pump task so replayed frames and live frames reach the outbound
    queue in that order — replaying from a second task could interleave a live
    seq 415 ahead of a replayed 413.
    """
    try:
        missed, complete = await realtime.replay(channel, since_seq, redis_client)
    except Exception as exc:                           # noqa: BLE001
        # Not fatal: the live subscription is already up, so the connection is
        # useful. What it must NOT do is carry on as if the resume succeeded —
        # `resync` is exactly the frame for "refetch over HTTP, you have a hole".
        log.warning("realtime_replay_failed", channel=channel, error=str(exc)[:200])
        state.offer({"type": "resync", "channel": channel,
                     "data": _resync_target(channel)})
        return
    if not complete:
        state.offer({"type": "resync", "channel": channel,
                     "data": _resync_target(channel)})
        return
    for item in missed:
        if not state.offer(item):
            state.stop(CLOSE_TOO_SLOW, "send_queue_full")
            return


def _resync_target(channel: str) -> dict:
    family, _, subject = channel.partition(":")
    template = REFETCH.get(family)
    if template is None:
        return {}
    return {"refetch": template.format(xid=subject)}


def _refuse(state: _Connection, code: str, message: str, *,
            channel: str | None = None, request_id: Any = None) -> None:
    """An `RtError`, carrying the contract's code and nothing else.

    The specific reason a channel was refused — "not this centre", "no such
    subject" — is logged and never sent. Told apart on the wire, those two are an
    oracle for enumerating which xids exist and who owns them.
    """
    state.offer({"type": "error", "id": request_id, "channel": channel,
                 "data": {"code": code, "message": message, "channel": channel}})


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    if websocket.client_state is WebSocketState.DISCONNECTED:
        return
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code, reason=reason)


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
