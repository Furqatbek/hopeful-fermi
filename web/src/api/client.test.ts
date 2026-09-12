import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, pendingReplays } from "./client";
import { clearSession, getAccessToken, refreshSession } from "./session";

vi.mock("./session", () => ({
  getAccessToken: vi.fn(),
  refreshSession: vi.fn(),
  clearSession: vi.fn(),
}));

/**
 * What fetch saw, per call. The body is read HERE, at call time, because that
 * is what a real fetch does: it consumes the request body, after which
 * `Request.clone()` throws. A mock that leaves the body untouched would let a
 * clone-after-fetch pass, which is exactly the defect these tests pin.
 */
type Seen = { method: string; url: string; auth: string | null; body: string };

const seen: Seen[] = [];
let answers: (() => Response)[] = [];

const BASE = "http://exam.test/api/v1";

function respond(status: number, body: unknown = {}): () => Response {
  return () => new Response(JSON.stringify(body), {
    status, headers: { "content-type": "application/json" },
  });
}

const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const request = input as Request;
  seen.push({
    method: request.method,
    url: request.url,
    auth: request.headers.get("Authorization"),
    body: await request.text(),
  });
  const next = answers.shift();
  if (!next) throw new Error("fetch called more times than the test scripted");
  return next();
});

beforeEach(() => {
  seen.length = 0;
  answers = [];
  vi.stubGlobal("fetch", fetchMock);
  vi.mocked(getAccessToken).mockReturnValue("stale");
  vi.mocked(refreshSession).mockImplementation(async () => {
    vi.mocked(getAccessToken).mockReturnValue("fresh");
    return true;
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  // Not `resetAllMocks`: that would strip `fetchMock` of its implementation.
  fetchMock.mockClear();
  vi.mocked(getAccessToken).mockReset();
  vi.mocked(refreshSession).mockReset();
  vi.mocked(clearSession).mockReset();
});

const DELTAS = [{
  question_version_xid: "6f1f2b9e-1d44-4a3a-9a8e-1d0a5c3b2f10",
  slot_key: "s1", response: "bicycle", client_seq: 3,
}];

describe("refreshing once and replaying on 401", () => {
  it("replays a POST with a JSON body, carrying the new bearer and the same body", async () => {
    // Before the clone moved into onRequest this rejected with a TypeError:
    // fetch had consumed the body, and `request.clone()` in onResponse threw.
    answers = [respond(401), respond(200, { accepted: 1, rejected: [], last_accepted_seq: 3 })];

    const { data, error } = await api.POST("/attempts/{xid}/answers", {
      baseUrl: BASE, fetch: fetchMock,
      params: { path: { xid: "att_1" }, header: { "Idempotency-Key": "k1" } },
      body: { deltas: DELTAS },
    });

    expect(error).toBeUndefined();
    expect(data?.accepted).toBe(1);
    expect(seen).toHaveLength(2);
    expect(seen[0]!.auth).toBe("Bearer stale");
    expect(seen[1]!.auth).toBe("Bearer fresh");
    expect(seen[1]!.method).toBe("POST");
    expect(seen[1]!.url).toBe(`${BASE}/attempts/att_1/answers`);
    expect(JSON.parse(seen[1]!.body)).toEqual({ deltas: DELTAS });
    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(pendingReplays()).toBe(0);
  });

  it("still replays a body-less GET", async () => {
    answers = [respond(401), respond(200, { xid: "u1" })];

    const { error } = await api.GET("/me", { baseUrl: BASE, fetch: fetchMock });

    expect(error).toBeUndefined();
    expect(seen.map((s) => s.auth)).toEqual(["Bearer stale", "Bearer fresh"]);
    expect(seen[1]!.body).toBe("");
    expect(pendingReplays()).toBe(0);
  });

  it("replays exactly once: a second 401 is the answer, not a timing problem", async () => {
    answers = [respond(401), respond(401, { title: "Forbidden" })];

    const { error } = await api.GET("/me", { baseUrl: BASE, fetch: fetchMock });

    expect(error).toBeDefined();
    expect(seen).toHaveLength(2);
    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(pendingReplays()).toBe(0);
  });

  it("signs out instead of replaying when the refresh fails", async () => {
    const dispatchEvent = vi.fn();
    vi.stubGlobal("window", { dispatchEvent });
    vi.stubGlobal("CustomEvent", class { constructor(public type: string) {} });
    vi.mocked(refreshSession).mockResolvedValue(false);
    answers = [respond(401)];

    const { error, response } = await api.GET("/me", { baseUrl: BASE, fetch: fetchMock });

    expect(error).toBeDefined();
    expect(response.status).toBe(401);
    expect(seen).toHaveLength(1);
    expect(clearSession).toHaveBeenCalledTimes(1);
    expect(dispatchEvent).toHaveBeenCalledTimes(1);
    expect(pendingReplays()).toBe(0);
  });
});

describe("the replay clone is never left behind", () => {
  it("is dropped after a normal response", async () => {
    answers = [respond(200, { xid: "u1" })];
    await api.GET("/me", { baseUrl: BASE, fetch: fetchMock });
    expect(seen).toHaveLength(1);
    expect(refreshSession).not.toHaveBeenCalled();
    expect(pendingReplays()).toBe(0);
  });

  it("is dropped when fetch itself fails", async () => {
    // A network failure never reaches onResponse; without onError the map
    // would keep one clone per dropped request for the life of the page.
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(api.GET("/me", { baseUrl: BASE, fetch: fetchMock })).rejects.toThrow("Failed to fetch");
    expect(pendingReplays()).toBe(0);
  });

  it("is not taken for an anonymous route", async () => {
    // These must not carry a token or trigger a refresh, so there is nothing
    // to replay and no reason to hold a copy.
    answers = [respond(401, { code: "invalid_token" })];
    const { error } = await api.POST("/auth/refresh", { baseUrl: BASE, fetch: fetchMock });
    expect(error).toBeDefined();
    expect(seen).toHaveLength(1);
    expect(seen[0]!.auth).toBeNull();
    expect(refreshSession).not.toHaveBeenCalled();
    expect(pendingReplays()).toBe(0);
  });
});
