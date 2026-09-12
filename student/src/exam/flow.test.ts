import { describe, expect, it, vi } from "vitest";

import {
  MAX_RETRY_MS, advancesOnSectionExpiry, claimsArrowKey, flushOnce, nextDelay, submitFlow,
  type FlushDeps, type SubmitDeps, type SubmitState,
} from "./flow";
import { MAX_BATCH, type Row } from "./outbox";

const row = (id: number, seq = 1, slot = "s1"): Row =>
  ({ id, question_version_xid: "q1", slot_key: slot, response: "x", client_seq: seq });

const clock = { remainingMs: 1000, takenAt: 0 };

function flushDeps(over: Partial<FlushDeps> = {}) {
  let n = 0;
  const queue: Row[][] = [];
  const deps = {
    pending: vi.fn(async () => queue.shift() ?? []),
    flush: vi.fn(async () => ({ accepted: 1, rejected: [], last_accepted_seq: 1, clock })),
    forget: vi.fn(async () => undefined),
    refuse: vi.fn(async () => undefined),
    mint: vi.fn(() => `key-${++n}`),
    ...over,
  };
  return { deps, queue };
}

describe("one pass of the flush loop", () => {
  it("does nothing, and reports drained, when nothing is waiting", async () => {
    const { deps } = flushDeps();
    const held = { key: "old", ids: [1] };
    const out = await flushOnce(deps, "a", held);
    expect(out).toEqual({ inFlight: held, refused: [], drained: true });
    expect(deps.flush).not.toHaveBeenCalled();
  });

  it("keeps the batch's key and touches nothing on disk when the network fails", async () => {
    // The key is what must survive a timeout: a retry that minted a fresh one
    // could apply the batch twice.
    const { deps, queue } = flushDeps({ flush: vi.fn(async () => { throw new Error("offline"); }) });
    queue.push([row(1), row(2)]);
    const prior = { key: "held", ids: [1, 2] };
    const out = await flushOnce(deps, "a", prior);
    expect(out.inFlight).toBe(prior);
    expect(out.drained).toBe(false);
    expect(out.clock).toBeUndefined();
    expect(deps.forget).not.toHaveBeenCalled();
    expect(deps.refuse).not.toHaveBeenCalled();
  });

  it("mints a key for a new batch and keeps it across the failure", async () => {
    const { deps, queue } = flushDeps({ flush: vi.fn(async () => { throw new Error("offline"); }) });
    queue.push([row(1)]);
    const out = await flushOnce(deps, "a", null);
    expect(out.inFlight).toEqual({ key: "key-1", ids: [1] });
    expect(deps.flush).toHaveBeenCalledWith("a", [row(1)], "key-1");
  });

  it("on success clears the key, deletes what was taken and marks what was refused", async () => {
    const { deps, queue } = flushDeps({
      flush: vi.fn(async () => ({
        accepted: 1,
        rejected: [{ question_version_xid: "q1", slot_key: "s2", reason: "stale_seq" }],
        last_accepted_seq: 1,
        clock,
      })),
    });
    queue.push([row(1, 1, "s1"), row(2, 1, "s2")], []);
    const out = await flushOnce(deps, "a", { key: "held", ids: [1, 2] });
    expect(out.inFlight).toBeNull();
    expect(out.clock).toBe(clock);
    expect(deps.forget).toHaveBeenCalledWith([1]);
    expect(deps.refuse).toHaveBeenCalledWith([{ id: 2, reason: "stale_seq" }]);
    expect(out.refused).toEqual([{ id: 2, reason: "stale_seq" }]);
    expect(out.drained).toBe(true);
  });

  it("is not drained when something was queued during the round trip", async () => {
    const { deps, queue } = flushDeps();
    queue.push([row(1)], [row(2)]);
    const out = await flushOnce(deps, "a", null);
    expect(out.inFlight).toBeNull();
    expect(out.drained).toBe(false);
  });

  it("collapses a backlog past a batch before sending it", async () => {
    const sent: (readonly Row[])[] = [];
    const { deps, queue } = flushDeps({
      flush: async (_xid, rows) => { sent.push(rows); return { accepted: 1, rejected: [], last_accepted_seq: 1, clock }; },
    });
    const many = Array.from({ length: MAX_BATCH + 50 }, (_, i) => row(i, i));
    queue.push(many, []);
    await flushOnce(deps, "a", null);
    expect(sent[0]).toHaveLength(1);
    expect(sent[0]![0]!.client_seq).toBe(MAX_BATCH + 49);
  });

  it("lets a storage failure propagate to the caller", async () => {
    const { deps } = flushDeps({ pending: vi.fn(async () => { throw new Error("no store"); }) });
    await expect(flushOnce(deps, "a", null)).rejects.toThrow("no store");
  });
});

function submitDeps(over: Partial<SubmitDeps> = {}) {
  let n = 0;
  const order: string[] = [];
  const deps = {
    flush: vi.fn(async () => { order.push("flush"); return true; }),
    pending: vi.fn(async () => [] as Row[]),
    submit: vi.fn(async () => { order.push("submit"); return {}; }),
    drop: vi.fn(async () => { order.push("drop"); }),
    mint: vi.fn(() => `key-${++n}`),
    ...over,
  };
  return { deps, order };
}

const fresh = (): SubmitState => ({ key: null, busy: false });

describe("submitting", () => {
  it("flushes before it submits, and drops only afterwards", async () => {
    const { deps, order } = submitDeps();
    const state = fresh();
    await expect(submitFlow(deps, "a", state)).resolves.toBe("submitted");
    expect(order).toEqual(["flush", "submit", "drop"]);
    expect(deps.submit).toHaveBeenCalledWith("a", "key-1", undefined);
  });

  it("retries the flush once, then carries what is still waiting in the body", async () => {
    // The flush before submit fails silently by design; the submit used to go
    // ahead and drop the undelivered rows. Now they ride as `final_answers`.
    const left = [row(7, 3), row(8, 1, "s2")];
    const { deps, order } = submitDeps({
      flush: vi.fn(async () => { order.push("flush"); return false; }),
      pending: vi.fn(async () => left),
    });
    await submitFlow(deps, "a", fresh());
    expect(deps.flush).toHaveBeenCalledTimes(2);
    expect(deps.submit).toHaveBeenCalledWith("a", "key-1", left);
    expect(order).toEqual(["flush", "flush", "submit", "drop"]);
  });

  it("does not read the outbox again once the flush drained on the retry", async () => {
    const { deps } = submitDeps({
      flush: vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true),
    });
    await submitFlow(deps, "a", fresh());
    expect(deps.pending).not.toHaveBeenCalled();
    expect(deps.submit).toHaveBeenCalledWith("a", "key-1", undefined);
  });

  it("never drops the rows when the submit throws, and unlatches for a retry", async () => {
    const { deps } = submitDeps({
      submit: vi.fn(async () => { throw new Error("502"); }),
    });
    const state = fresh();
    await expect(submitFlow(deps, "a", state)).rejects.toThrow("502");
    expect(deps.drop).not.toHaveBeenCalled();
    expect(state.busy).toBe(false);
    expect(state.key).toEqual({ key: "key-1", ids: [] });
  });

  it("still counts as submitted when only the clear-down of the store fails", async () => {
    const { deps } = submitDeps({ drop: vi.fn(async () => { throw new Error("no store"); }) });
    const state = fresh();
    await expect(submitFlow(deps, "a", state)).resolves.toBe("submitted");
    expect(state.busy).toBe(true);
  });

  /** A submit that times out once and lands the second time, recording keys. */
  function flakySubmit() {
    const keys: string[] = [];
    const submit = async (_xid: string, key: string) => {
      keys.push(key);
      if (keys.length === 1) throw new Error("timeout");
      return {};
    };
    return { keys, submit };
  }

  it("carries the same key on a retry whose remainder is unchanged", async () => {
    const left = [row(7, 3)];
    const { keys, submit } = flakySubmit();
    const { deps } = submitDeps({
      flush: vi.fn(async () => false),
      pending: vi.fn(async () => left),
      submit,
    });
    const state = fresh();
    await expect(submitFlow(deps, "a", state)).rejects.toThrow("timeout");
    await submitFlow(deps, "a", state);
    expect(keys).toEqual(["key-1", "key-1"]);
  });

  it("mints a fresh key when the remainder changed between tries", async () => {
    // Reusing a key with a different body is `409 idempotency_key_reused`;
    // a fresh one is safe because an already-submitted attempt returns its
    // current run rather than scoring again.
    const reads = [[row(7, 3)], [row(7, 3), row(9, 4)]];
    const { keys, submit } = flakySubmit();
    const { deps } = submitDeps({
      flush: vi.fn(async () => false),
      pending: vi.fn(async () => reads.shift() ?? []),
      submit,
    });
    const state = fresh();
    await expect(submitFlow(deps, "a", state)).rejects.toThrow("timeout");
    await submitFlow(deps, "a", state);
    expect(keys).toEqual(["key-1", "key-2"]);
  });

  it("is a no-op while one is already in flight", async () => {
    let release!: () => void;
    const { deps } = submitDeps({
      submit: vi.fn(() => new Promise<unknown>((resolve) => { release = () => resolve({}); })),
    });
    const state = fresh();
    const first = submitFlow(deps, "a", state);
    await Promise.resolve();
    await Promise.resolve();
    await expect(submitFlow(deps, "a", state)).resolves.toBe("busy");
    release();
    await expect(first).resolves.toBe("submitted");
    expect(deps.submit).toHaveBeenCalledTimes(1);
    // And stays latched once the paper is gone.
    await expect(submitFlow(deps, "a", state)).resolves.toBe("busy");
  });
});

describe("the timer-zero back-off", () => {
  it("fires the first try at once, then doubles from two seconds", () => {
    expect(nextDelay(0)).toBe(0);
    expect(nextDelay(1)).toBe(2_000);
    expect(nextDelay(2)).toBe(4_000);
    expect(nextDelay(3)).toBe(8_000);
    expect(nextDelay(4)).toBe(16_000);
  });

  it("caps at thirty seconds", () => {
    expect(nextDelay(5)).toBe(MAX_RETRY_MS);
    expect(nextDelay(20)).toBe(MAX_RETRY_MS);
  });

  it("stops on a voided attempt, which no retry can change", () => {
    expect(nextDelay(1, "attempt_voided")).toBeNull();
    expect(nextDelay(0, "attempt_voided")).toBeNull();
  });

  it("keeps trying through everything else", () => {
    expect(nextDelay(1, "attempt_expired")).toBe(2_000);
    expect(nextDelay(1, undefined)).toBe(2_000);
  });
});

describe("advancing when a section's own clock runs out", () => {
  const base = {
    sectionExpiresAt: "2026-08-13T10:20:00Z",
    attemptExpiresAt: "2026-08-13T11:00:00Z",
    position: 1,
    highestEntered: 1,
    isLast: false,
  };

  it("moves on from the furthest section entered when it is not the last", () => {
    expect(advancesOnSectionExpiry(base)).toBe(true);
  });

  it("does nothing for a section without its own deadline", () => {
    expect(advancesOnSectionExpiry({ ...base, sectionExpiresAt: null })).toBe(false);
    expect(advancesOnSectionExpiry({ ...base, sectionExpiresAt: undefined })).toBe(false);
  });

  it("leaves the attempt clock in charge when the deadlines coincide", () => {
    expect(advancesOnSectionExpiry({ ...base, sectionExpiresAt: base.attemptExpiresAt })).toBe(false);
  });

  it("never bounces a student who navigated back into a closed section", () => {
    expect(advancesOnSectionExpiry({ ...base, position: 1, highestEntered: 2 })).toBe(false);
  });

  it("never advances from the last section — the paper clock submits", () => {
    expect(advancesOnSectionExpiry({ ...base, isLast: true })).toBe(false);
  });
});

describe("which arrow keys the palette may take", () => {
  const el = (tagName: string, attrs: Record<string, string> = {}, editable = false) => ({
    tagName, isContentEditable: editable,
    getAttribute: (name: string) => attrs[name] ?? null,
  });

  it("steps from the shell, a button or nothing focused", () => {
    expect(claimsArrowKey(el("DIV"), false)).toBe(false);
    expect(claimsArrowKey(el("BUTTON"), false)).toBe(false);
    expect(claimsArrowKey(null, false)).toBe(false);
  });

  it("yields to a text field, as it always did", () => {
    expect(claimsArrowKey(el("INPUT"), false)).toBe(true);
    expect(claimsArrowKey(el("textarea"), false)).toBe(true);
  });

  it("yields to a select, which picks a value on Left/Right", () => {
    expect(claimsArrowKey(el("SELECT"), false)).toBe(true);
  });

  it("yields to the reading divider, by role and by its preventDefault", () => {
    expect(claimsArrowKey(el("DIV", { role: "separator" }), false)).toBe(true);
    expect(claimsArrowKey(el("DIV"), true)).toBe(true);
  });

  it("yields to anything contenteditable", () => {
    expect(claimsArrowKey(el("DIV", {}, true), false)).toBe(true);
  });
});
