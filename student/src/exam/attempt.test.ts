import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { submit } from "./attempt";

vi.mock("../api/session", () => ({
  getAccessToken: vi.fn(),
  refreshSession: vi.fn(),
  clearSession: vi.fn(),
}));

/**
 * What `submit` asks the client to send. The client itself is proved against
 * fetch in `client.test.ts`; this is the one layer between `submitFlow` (which
 * is handed a fake) and the wire, and the `final_answers` body is the only thing
 * on it that `tsc` cannot miss for us — `body` is optional in the generated
 * type, so a submit that dropped it would still build.
 *
 * `api.POST` is generic over the path, which is what makes its spy's recorded
 * calls unreadable to the type system; they are read back as the plain pairs
 * they are.
 */
function posted() {
  const post = vi.spyOn(api, "POST").mockResolvedValue(
    { data: { xid: "a", status: "submitted" }, response: new Response() });
  const calls = () => (post.mock.calls as unknown as [string, unknown][])
    .map(([path, init]) => ({ path, init }));
  return { post, calls };
}

afterEach(() => { vi.restoreAllMocks(); });

const ROW = {
  id: 7, question_version_xid: "6f1f2b9e-1d44-4a3a-9a8e-1d0a5c3b2f10",
  slot_key: "s1", response: "bicycle", client_seq: 3,
};

describe("submitting an attempt", () => {
  it("carries what the flush could not deliver as `final_answers`", async () => {
    const { calls } = posted();
    await submit("a", "k", [ROW]);
    expect(calls()).toStrictEqual([{
      path: "/attempts/{xid}/submit",
      init: {
        params: { path: { xid: "a" }, header: { "Idempotency-Key": "k" } },
        body: { final_answers: [ROW] },
      },
    }]);
  });

  it("sends no body at all when there is nothing left to carry", async () => {
    // Byte-for-byte what a submit always was, so the idempotent replay of a
    // drained submit matches the first.
    const { calls } = posted();
    await submit("a", "k");
    await submit("a", "k", []);
    expect(calls()).toHaveLength(2);
    for (const { init } of calls()) {
      expect(init).toStrictEqual({
        params: { path: { xid: "a" }, header: { "Idempotency-Key": "k" } },
      });
    }
  });

  it("throws the problem document when the server refuses", async () => {
    vi.spyOn(api, "POST").mockResolvedValue(
      { error: { code: "attempt_voided" }, response: new Response() });
    await expect(submit("a", "k")).rejects.toEqual({ code: "attempt_voided" });
  });
});
