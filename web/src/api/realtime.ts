/**
 * The WebSocket gateway, as a *connection* rather than as a socket.
 *
 * `POST /api/v1/realtime/ticket` mints a **single-use, ~30 s** ticket bound to
 * the user, because a browser cannot set headers on a WebSocket handshake and
 * the access token must not travel in a query string where it lands in every
 * proxy log. Single-use is the fact this module is shaped around:
 *
 *   **A cached ticket works exactly once.**
 *
 * A helper that mints on first use and reuses the token would connect, drop on
 * the first tunnel outage — routine on these networks — and then be refused for
 * the rest of the session with a close code indistinguishable from a dead
 * server. So `mintTicket` is called on EVERY connect, including every retry, and
 * the token is never stored anywhere in this module.
 *
 * ## Why the URL is derived here and not taken from the ticket response
 *
 * The ticket response carries a `url`, and it is not trustworthy: the gateway is
 * mounted at `/realtime` (deliberately outside `/api/v1`, since a socket is not
 * an HTTP operation and `check_api_coverage.py` compares that prefix against the
 * OpenAPI document), while `docker-compose.yml` sets
 * `REALTIME_URL: wss://${DOMAIN}/api/v1/realtime` — a path the application does
 * not serve. Same-origin is also what the rest of this client already assumes:
 * Caddy proxies `/api/*` and `/realtime` from one origin, so there is no CORS
 * and no environment variable to get wrong.
 *
 * ## Reconnection is honest or it is worse than nothing
 *
 * Three properties, and each exists to stop one failure:
 *
 *   * **A new ticket per connect** — see above.
 *   * **Bounded backoff with jitter.** Uncapped retries turn a brief gateway
 *     restart into a self-inflicted flood; no jitter means every console in the
 *     country reconnects on the same second.
 *   * **A status callback on every transition.** The one outcome this must never
 *     produce is a socket that is quietly dead while the screen looks live. A
 *     moderation queue that stopped receiving is indistinguishable from a quiet
 *     one unless the connection says which it is.
 *
 * No React, no dependencies, no DOM types in the state machine — the socket
 * arrives through `open`, so the whole thing is testable without a browser.
 */

/** Mounted at `/realtime`, NOT under `/api/v1`. See the note above. */
export const GATEWAY_PATH = "/realtime";

export const BASE_BACKOFF_MS = 1_000;
export const MAX_BACKOFF_MS = 30_000;

/** §4.6: the server pings every 25 s and the client gives up after two missed. */
const DEFAULT_HEARTBEAT_SECONDS = 25;
const MISSED_HEARTBEATS_BEFORE_DROP = 2;

/** The two codes a subscribe refusal may carry.
 *
 *  Both are treated identically and neither is shown to anyone. The server sends
 *  the same answer for "no such subject" and "not yours" precisely so a client
 *  cannot enumerate which ids are real, one refusal at a time — a UI that split
 *  them back into two messages would hand that oracle to whoever is reading the
 *  screen. */
const REFUSALS = ["forbidden_channel", "unknown_channel"];

/** §4.3's envelope. `data` is deliberately loose: this module routes frames and
 *  never interprets one. */
export interface Frame {
  type: string;
  id?: string | null;
  channel?: string | null;
  seq?: number;
  ts?: string;
  data?: Record<string, unknown>;
}

export interface SocketHandlers {
  open(): void;
  message(payload: string): void;
  closed(code: number, reason: string): void;
}

export interface ClosableSocket {
  send(payload: string): void;
  close(): void;
}

export type OpenSocket = (url: string, handlers: SocketHandlers) => ClosableSocket;

export type StreamState = "connecting" | "live" | "retrying" | "closed";

export interface StreamStatus {
  state: StreamState;
  /** Consecutive failed connections; back to 0 the moment a `hello` arrives. */
  attempt: number;
  /** Channels this connection asked for and did not get. Reset per connection,
   *  so a role granted a minute ago is picked up by the next reconnect. */
  refused: string[];
}

export interface StreamOptions {
  /** `ws(s)://host/realtime`, from `socketUrl`. */
  url: string;
  channels: string[];
  /** Called before EVERY connect. Must return a fresh ticket. */
  mintTicket: () => Promise<string>;
  onFrame: (frame: Frame) => void;
  onStatus: (status: StreamStatus) => void;
  /** Injected in tests. Defaults to a real WebSocket. */
  open?: OpenSocket;
}

export interface Stream {
  close(): void;
}

/**
 * The gateway URL for the page this console is served from.
 *
 * Same origin as the API, which is what makes the whole client free of a
 * `VITE_API_URL`. Note that Vite's dev proxy forwards `/api` only — `/realtime`
 * needs its own `ws: true` entry or the handshake in development is answered by
 * the dev server rather than the application.
 */
export function socketUrl(href: string): string {
  const page = new URL(href);
  const scheme = page.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${page.host}${GATEWAY_PATH}`;
}

/**
 * How long to wait before retry number `attempt` (1-based).
 *
 * Exponential to a ceiling, then jittered down by up to half. The ceiling stops
 * a gateway restart becoming a retry flood; the jitter stops every console that
 * dropped together from reconnecting on the same second, which would rebuild the
 * thundering herd the backoff exists to prevent.
 */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const step = Math.min(MAX_BACKOFF_MS,
                        BASE_BACKOFF_MS * 2 ** Math.max(0, attempt - 1));
  return Math.round(step * (0.5 + random() * 0.5));
}

function parseFrame(payload: string): Frame | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const frame = parsed as Frame;
  return typeof frame.type === "string" ? frame : null;
}

function browserSocket(url: string, handlers: SocketHandlers): ClosableSocket {
  const socket = new WebSocket(url);
  socket.onopen = () => handlers.open();
  socket.onmessage = (event) => handlers.message(String(event.data));
  // No `onerror` handler on purpose. A WebSocket error is always followed by a
  // close, so handling both would schedule two retries for one failure.
  socket.onclose = (event) => handlers.closed(event.code, event.reason);
  return {
    send: (payload) => socket.send(payload),
    close: () => socket.close(),
  };
}

/**
 * Hold a subscription to `channels`, across drops, until `close()`.
 *
 * Returns immediately; the first ticket is minted asynchronously and every
 * transition is reported through `onStatus`.
 */
export function connect(options: StreamOptions): Stream {
  const open = options.open ?? browserSocket;

  let socket: ClosableSocket | null = null;
  let attempt = 0;
  let refused: string[] = [];
  let stopped = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let heartbeatSeconds = DEFAULT_HEARTBEAT_SECONDS;
  let lastHeard = 0;
  // Which connection a callback belongs to. A socket that is closing still
  // delivers queued events, and without this a dying connection's `closed`
  // schedules a second retry alongside the one already running — two sockets,
  // both reconnecting, neither aware of the other.
  let generation = 0;

  function announce(state: StreamState): void {
    options.onStatus({ state, attempt, refused: [...refused] });
  }

  function stopHeartbeat(): void {
    if (heartbeatTimer !== null) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function drop(): void {
    stopHeartbeat();
    const dying = socket;
    socket = null;
    dying?.close();
  }

  function scheduleRetry(): void {
    if (stopped) return;
    attempt += 1;
    announce("retrying");
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void start();
    }, backoffDelay(attempt));
  }

  function startHeartbeat(): void {
    stopHeartbeat();
    lastHeard = Date.now();
    heartbeatTimer = setInterval(() => {
      const silent = Date.now() - lastHeard;
      if (silent > heartbeatSeconds * 1000 * (MISSED_HEARTBEATS_BEFORE_DROP + 0.5)) {
        // Two missed heartbeats. A half-open TCP connection looks exactly like a
        // quiet one at this layer, and waiting for the kernel to notice takes
        // hours — during which the queue silently stops updating.
        generation += 1;
        drop();
        scheduleRetry();
        return;
      }
      // The server reaps a connection it has not heard from either, and any
      // inbound frame is what stamps its liveness clock. Without this the socket
      // is closed from the other end every heartbeat window.
      socket?.send(JSON.stringify({ type: "ping" }));
    }, heartbeatSeconds * 1000);
  }

  function handle(frame: Frame, mine: number): void {
    if (mine !== generation) return;
    lastHeard = Date.now();

    if (frame.type === "hello") {
      const seconds = frame.data?.["heartbeat_seconds"];
      if (typeof seconds === "number" && seconds > 0) heartbeatSeconds = seconds;
      attempt = 0;
      announce("live");
      startHeartbeat();
      // Asked for unconditionally, including channels the server has already
      // granted implicitly. The gateway answers a channel it is already holding
      // out of its own set without re-authorizing it, so this costs one frame
      // and removes the need to trust an undeclared `hello` field to know which
      // ones those were.
      socket?.send(JSON.stringify({ type: "subscribe", channels: options.channels }));
      return;
    }

    if (frame.type === "error") {
      const code = frame.data?.["code"];
      const channel = frame.channel ?? null;
      if (typeof code === "string" && REFUSALS.includes(code)
          && typeof channel === "string" && !refused.includes(channel)) {
        refused = [...refused, channel];
        announce("live");
      }
      // Not retried. A refusal is an answer, not a failure, and asking again on
      // the same connection would be a log line per attempt in the one log where
      // a repeated refused subscribe to `safety` is a signal somebody reads.
    }

    // Everything else — `subscribed`, `resync`, `ping` and the channel frames
    // themselves — is the caller's business. `resync` in particular must not be
    // swallowed here: it is the server saying "you have a hole, refetch over
    // HTTP", and the whole point of it being explicit is that somebody acts.
    options.onFrame(frame);
  }

  async function start(): Promise<void> {
    if (stopped) return;
    generation += 1;
    const mine = generation;
    refused = [];
    announce("connecting");

    let ticket: string;
    try {
      ticket = await options.mintTicket();
    } catch {
      // A ticket cannot be minted when Redis is unreachable — the endpoint
      // answers 503 rather than issuing one the gateway could not verify. That
      // is a retry, not an error to render: the rest of the API is unaffected
      // and the queue below keeps loading over HTTP.
      if (mine === generation) scheduleRetry();
      return;
    }
    if (stopped || mine !== generation) return;

    socket = open(`${options.url}?ticket=${encodeURIComponent(ticket)}`, {
      open: () => {
        // Deliberately nothing. The connection is not usable until `hello`
        // arrives; treating the TCP upgrade as success would show "live" for a
        // socket the gateway is about to close with `ticket_invalid`.
      },
      message: (payload) => {
        const frame = parseFrame(payload);
        if (frame) handle(frame, mine);
      },
      closed: () => {
        if (mine !== generation) return;
        stopHeartbeat();
        socket = null;
        scheduleRetry();
      },
    });
  }

  void start();

  return {
    close(): void {
      stopped = true;
      generation += 1;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      drop();
      announce("closed");
    },
  };
}
