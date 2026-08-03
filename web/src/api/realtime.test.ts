import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  BASE_BACKOFF_MS,
  MAX_BACKOFF_MS,
  backoffDelay,
  connect,
  socketUrl,
} from "./realtime";
import type { ClosableSocket, SocketHandlers, StreamStatus } from "./realtime";

describe("socketUrl", () => {
  it("upgrades the page scheme rather than guessing one", () => {
    // `ws:` on an `https:` page is blocked as mixed content, and the symptom is
    // a handshake that fails with no error the console can read.
    expect(socketUrl("https://hub.example.uz/moderation")).toBe(
      "wss://hub.example.uz/realtime");
    expect(socketUrl("http://localhost:5173/moderation")).toBe(
      "ws://localhost:5173/realtime");
  });

  it("keeps the port, because development is not on 443", () => {
    expect(socketUrl("http://127.0.0.1:5173/x")).toBe("ws://127.0.0.1:5173/realtime");
  });

  it("is not under the API prefix", () => {
    // The gateway is mounted at `/realtime`; `docker-compose.yml` advertises
    // `/api/v1/realtime`, which the application does not serve.
    expect(socketUrl("https://hub.example.uz/")).not.toContain("/api/");
  });
});

describe("backoffDelay", () => {
  const half = () => 0;
  const full = () => 1;

  it("grows, and never past the ceiling", () => {
    expect(backoffDelay(1, full)).toBe(BASE_BACKOFF_MS);
    expect(backoffDelay(2, full)).toBe(BASE_BACKOFF_MS * 2);
    expect(backoffDelay(40, full)).toBe(MAX_BACKOFF_MS);
  });

  it("jitters downward by at most half", () => {
    // A fixed delay means every console that dropped together reconnects on the
    // same second, which is the herd the backoff exists to break up.
    expect(backoffDelay(3, half)).toBe(BASE_BACKOFF_MS * 2);
    expect(backoffDelay(3, full)).toBe(BASE_BACKOFF_MS * 4);
  });

  it("always waits, so a failing connect cannot spin", () => {
    for (let attempt = 1; attempt <= 12; attempt += 1) {
      expect(backoffDelay(attempt, half)).toBeGreaterThanOrEqual(BASE_BACKOFF_MS / 2);
      expect(backoffDelay(attempt, full)).toBeLessThanOrEqual(MAX_BACKOFF_MS);
    }
  });
});

/** A socket the test drives by hand. */
class FakeSocket implements ClosableSocket {
  sent: string[] = [];
  gone = false;

  constructor(readonly url: string, private readonly handlers: SocketHandlers) {}

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(): void {
    if (this.gone) return;
    this.gone = true;
    this.handlers.closed(1000, "");
  }

  /** The server sends a frame. */
  deliver(frame: object): void {
    this.handlers.message(JSON.stringify(frame));
  }

  /** Whatever arrived on the wire, JSON or not. */
  raw(payload: string): void {
    this.handlers.message(payload);
  }

  /** The connection dies without a close handshake, as a dropped tunnel does. */
  fail(code = 1006): void {
    if (this.gone) return;
    this.gone = true;
    this.handlers.closed(code, "");
  }

  frames(): { type: string; channels?: string[] }[] {
    return this.sent.map((sent) =>
      JSON.parse(sent) as { type: string; channels?: string[] });
  }
}

function harness(options: { tickets?: string[]; mintFails?: boolean } = {}) {
  const sockets: FakeSocket[] = [];
  const states: StreamStatus[] = [];
  const frames: { type: string }[] = [];
  const minted: string[] = [];
  let issued = 0;

  const stream = connect({
    url: "wss://hub.example.uz/realtime",
    channels: ["safety"],
    mintTicket: async () => {
      if (options.mintFails) throw new Error("realtime_unavailable");
      issued += 1;
      const ticket = options.tickets?.[issued - 1] ?? `t${issued}`;
      minted.push(ticket);
      return ticket;
    },
    onFrame: (frame) => frames.push(frame),
    onStatus: (status) => states.push(status),
    open: (url, handlers) => {
      const socket = new FakeSocket(url, handlers);
      sockets.push(socket);
      return socket;
    },
  });

  return { stream, sockets, states, frames, minted };
}

const hello = { type: "hello", data: { heartbeat_seconds: 25 } };
const settle = () => vi.advanceTimersByTimeAsync(0);

describe("connect", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("puts the ticket in the URL and nowhere else", async () => {
    const { stream, sockets } = harness();
    await settle();
    expect(sockets[0]?.url).toBe("wss://hub.example.uz/realtime?ticket=t1");
    stream.close();
  });

  it("mints a NEW ticket for every connection", async () => {
    // The whole reason this module exists. A ticket is single-use and ~30 s, so
    // a helper that cached one would connect, drop on the first tunnel outage
    // and then be refused for the rest of the session — with a close code that
    // looks exactly like a dead server.
    const { stream, sockets, minted } = harness();
    await settle();
    sockets[0]?.deliver(hello);
    sockets[0]?.fail();

    await vi.advanceTimersByTimeAsync(MAX_BACKOFF_MS);
    expect(sockets).toHaveLength(2);
    expect(minted).toEqual(["t1", "t2"]);
    expect(sockets[1]?.url).toContain("ticket=t2");
    stream.close();
  });

  it("is not live until the gateway says hello", async () => {
    // The TCP upgrade succeeds even when the ticket has already been burned:
    // the gateway accepts, then closes with 4401. Reporting "live" on open
    // would show a connected queue that is about to go away.
    const { stream, sockets, states } = harness();
    await settle();
    expect(states.map((s) => s.state)).toEqual(["connecting"]);

    sockets[0]?.deliver(hello);
    expect(states.at(-1)?.state).toBe("live");
    stream.close();
  });

  it("subscribes once the connection is usable", async () => {
    const { stream, sockets } = harness();
    await settle();
    sockets[0]?.deliver(hello);
    expect(sockets[0]?.frames()).toEqual([{ type: "subscribe", channels: ["safety"] }]);
    stream.close();
  });

  it("records a refusal without asking again", async () => {
    const { stream, sockets, states } = harness();
    await settle();
    sockets[0]?.deliver(hello);
    sockets[0]?.deliver({
      type: "error", channel: "safety",
      data: { code: "forbidden_channel", message: "You may not subscribe to that channel." },
    });

    expect(states.at(-1)?.refused).toEqual(["safety"]);
    // One `subscribe`, plus nothing. Retrying a refusal on the same connection
    // is a log line per attempt in the one log where a repeated refused
    // subscribe to `safety` is something a human reads.
    expect(sockets[0]?.frames().filter((f) => f.type === "subscribe")).toHaveLength(1);
    stream.close();
  });

  it("treats both refusal codes the same", async () => {
    // The server answers `forbidden_channel` for "not yours" and for "no such
    // subject" on purpose. Telling them apart in a UI would rebuild the
    // enumeration oracle the server refuses to provide.
    const { stream, sockets, states } = harness();
    await settle();
    sockets[0]?.deliver(hello);
    sockets[0]?.deliver({ type: "error", channel: "safety",
                          data: { code: "unknown_channel" } });
    expect(states.at(-1)?.refused).toEqual(["safety"]);
    stream.close();
  });

  it("forgets refusals on the next connection", async () => {
    // A platform-admin grant made a minute ago should take effect on reconnect
    // rather than needing a reload.
    const { stream, sockets, states } = harness();
    await settle();
    sockets[0]?.deliver(hello);
    sockets[0]?.deliver({ type: "error", channel: "safety",
                          data: { code: "forbidden_channel" } });
    sockets[0]?.fail();

    await vi.advanceTimersByTimeAsync(MAX_BACKOFF_MS);
    sockets[1]?.deliver(hello);
    expect(states.at(-1)?.refused).toEqual([]);
    stream.close();
  });

  it("passes resync through instead of swallowing it", async () => {
    const { stream, sockets, frames } = harness();
    await settle();
    sockets[0]?.deliver(hello);
    sockets[0]?.deliver({ type: "resync", channel: "safety",
                          data: { refetch: "/api/v1/admin/reports" } });
    expect(frames.map((f) => f.type)).toContain("resync");
    stream.close();
  });

  it("retries when no ticket can be minted", async () => {
    // 503 `realtime_unavailable` means Redis is unreachable, not that the admin
    // did something wrong. The queue below still loads over HTTP.
    const { stream, sockets, states } = harness({ mintFails: true });
    await settle();
    expect(sockets).toHaveLength(0);
    expect(states.at(-1)?.state).toBe("retrying");

    await vi.advanceTimersByTimeAsync(MAX_BACKOFF_MS);
    expect(states.at(-1)?.state).toBe("retrying");
    stream.close();
  });

  it("gives up when the caller closes it", async () => {
    const { stream, sockets, states } = harness();
    await settle();
    sockets[0]?.deliver(hello);
    stream.close();

    await vi.advanceTimersByTimeAsync(MAX_BACKOFF_MS * 4);
    expect(sockets).toHaveLength(1);
    expect(states.at(-1)?.state).toBe("closed");
  });

  it("reconnects when the gateway goes silent", async () => {
    // A half-open TCP connection is indistinguishable from a quiet one, and
    // waiting for the kernel to notice takes hours — hours in which the queue
    // looks live and receives nothing.
    const { stream, sockets } = harness();
    await settle();
    sockets[0]?.deliver(hello);

    await vi.advanceTimersByTimeAsync(25_000);
    expect(sockets[0]?.frames().at(-1)).toEqual({ type: "ping" });

    await vi.advanceTimersByTimeAsync(60_000 + MAX_BACKOFF_MS);
    expect(sockets.length).toBeGreaterThan(1);
    stream.close();
  });

  it("drops a frame that is not a frame rather than throwing", async () => {
    // Nothing in the product sends these, but an exception raised inside the
    // socket callback would leave the connection open and the reconnect logic
    // never reached — a stream that is dead without ever reporting it.
    const { stream, sockets, frames, states } = harness();
    await settle();
    sockets[0]?.deliver(hello);
    const before = frames.length;

    expect(() => sockets[0]?.raw("<html>502 Bad Gateway</html>")).not.toThrow();
    expect(() => sockets[0]?.raw("[1,2,3]")).not.toThrow();
    expect(() => sockets[0]?.raw('{"no":"type"}')).not.toThrow();

    expect(frames).toHaveLength(before);
    expect(states.at(-1)?.state).toBe("live");
    stream.close();
  });
});
