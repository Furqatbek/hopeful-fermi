import { describe, expect, it } from "vitest";

import { MAX_BATCH, batch, collapse, nextSeq, slotKey } from "./outbox";
import * as outbox from "./outbox";

const row = (over: Partial<{
  id: number; question_version_xid: string; slot_key: string;
  response: string | string[] | null; client_seq: number;
}> = {}) => ({
  id: 1, question_version_xid: "q1", slot_key: "s1",
  response: "x", client_seq: 1, ...over,
});

describe("sequence numbers, which are what make a blind retry safe", () => {
  it("starts at 1 for a slot never touched", () => {
    expect(nextSeq({}, "q1:s1")).toBe(1);
  });

  it("increases per slot, independently", () => {
    const seqs = { "q1:s1": 3, "q1:s2": 1 };
    expect(nextSeq(seqs, "q1:s1")).toBe(4);
    expect(nextSeq(seqs, "q1:s2")).toBe(2);
    expect(nextSeq(seqs, "q2:s1")).toBe(1);
  });

  it("keys by question AND slot, so two questions cannot share a counter", () => {
    expect(slotKey("q1", "s1")).toBe("q1:s1");
    expect(slotKey("q2", "s1")).not.toBe(slotKey("q1", "s1"));
  });
});

describe("batching", () => {
  it("caps at the 200 the contract permits", () => {
    const many = Array.from({ length: 250 }, (_, i) => row({ id: i, client_seq: i }));
    expect(batch(many)).toHaveLength(MAX_BATCH);
  });

  it("sends the OLDEST first, so the queue drains in order", () => {
    const many = Array.from({ length: 250 }, (_, i) => row({ id: i, client_seq: i }));
    expect(batch(many)[0]!.id).toBe(0);
  });

  it("does not mutate the queue it was handed", () => {
    const rows = [row({ id: 2 }), row({ id: 1 })];
    batch(rows);
    expect(rows.map((r) => r.id)).toEqual([2, 1]);
  });
});

describe("collapsing a backlog", () => {
  it("keeps only the newest delta per slot", () => {
    // Ten minutes of typing offline into one box is ten rows the server would
    // discard all but the last of anyway.
    const rows = [
      row({ id: 1, client_seq: 1, response: "b" }),
      row({ id: 2, client_seq: 2, response: "bi" }),
      row({ id: 3, client_seq: 3, response: "bicycle" }),
    ];
    const out = collapse(rows);
    expect(out).toHaveLength(1);
    expect(out[0]!.response).toBe("bicycle");
    expect(out[0]!.client_seq).toBe(3);
  });

  it("keeps every distinct slot", () => {
    const rows = [
      row({ id: 1, slot_key: "s1", client_seq: 1 }),
      row({ id: 2, slot_key: "s2", client_seq: 1 }),
      row({ id: 3, slot_key: "s1", client_seq: 2 }),
    ];
    expect(collapse(rows)).toHaveLength(2);
  });

  it("keeps slots of the same name on DIFFERENT questions apart", () => {
    // Every question has an `s1`. Collapsing on slot name alone would throw away
    // one answer per question, which is the worst possible bug in this file.
    const rows = [
      row({ id: 1, question_version_xid: "q1", slot_key: "s1", response: "one" }),
      row({ id: 2, question_version_xid: "q2", slot_key: "s1", response: "two" }),
    ];
    const out = collapse(rows);
    expect(out).toHaveLength(2);
    expect(out.map((r) => r.response).sort()).toEqual(["one", "two"]);
  });

  it("preserves queue order rather than first-touched order", () => {
    const rows = [
      row({ id: 5, slot_key: "s2", client_seq: 1 }),
      row({ id: 6, slot_key: "s1", client_seq: 1 }),
      row({ id: 7, slot_key: "s2", client_seq: 2 }),
    ];
    expect(collapse(rows).map((r) => r.id)).toEqual([6, 7]);
  });

  it("never drops an answer that was not superseded by a higher seq", () => {
    const rows = [
      row({ id: 1, slot_key: "s1", client_seq: 9, response: "kept" }),
      row({ id: 2, slot_key: "s1", client_seq: 2, response: "stale" }),
    ];
    const out = collapse(rows);
    expect(out).toHaveLength(1);
    expect(out[0]!.response).toBe("kept");
  });

  it("handles an empty queue", () => {
    expect(collapse([])).toEqual([]);
  });
});

describe("what to delete after a flush, and what to keep", () => {
  // The whole batch was deleted, rejections included — so an answer the server
  // declined was gone from disk, gone from the server, and present only in
  // React state, which survives exactly until the reload this module exists
  // for.
  const row = (id: number, q: string, slot: string, seq = 1) =>
    ({ id, question_version_xid: q, slot_key: slot, response: "x", client_seq: seq });

  it("deletes what the server took", () => {
    const rows = [row(1, "qv-1", "s1"), row(2, "qv-2", "s1")];
    expect(outbox.partition(rows, [])).toEqual({ accepted: [1, 2], refused: [] });
  });

  it("keeps what it refused, with the reason", () => {
    const rows = [row(1, "qv-1", "s1"), row(2, "qv-2", "s1")];
    const { accepted, refused } = outbox.partition(rows, [
      { question_version_xid: "qv-2", slot_key: "s1", reason: "schema_invalid" },
    ]);
    expect(accepted).toEqual([1]);
    expect(refused).toEqual([{ id: 2, reason: "schema_invalid" }]);
  });

  it("refuses every row for a refused slot, not just the last", () => {
    // A student who typed twice between flushes has two rows for one slot at
    // two sequence numbers. The server declined the SLOT; keeping one of them
    // queued would retry a delta that can never be accepted.
    const rows = [row(1, "qv-1", "s1", 1), row(2, "qv-1", "s1", 2),
                  row(3, "qv-1", "s2", 1)];
    const { accepted, refused } = outbox.partition(rows, [
      { question_version_xid: "qv-1", slot_key: "s1", reason: "stale_seq" },
    ]);
    expect(accepted).toEqual([3]);
    expect(refused.map((r) => r.id)).toEqual([1, 2]);
  });

  it("does not confuse one slot with another on the same question", () => {
    const rows = [row(1, "qv-1", "s1"), row(2, "qv-1", "s2")];
    const { accepted } = outbox.partition(rows, [
      { question_version_xid: "qv-1", slot_key: "s2" },
    ]);
    expect(accepted).toEqual([1]);
  });
});

describe("the idempotency key a retry carries", () => {
  // It was minted inline at the call site, so every attempt at the same batch
  // carried a fresh UUID: the header was sent and the server could not
  // recognise a retry as one.
  let n = 0;
  const mint = () => `key-${++n}`;

  it("is minted once for a batch", () => {
    n = 0;
    const first = outbox.flushKey(null, [1, 2, 3], mint);
    expect(first.key).toBe("key-1");
  });

  it("is REUSED when the same batch goes again", () => {
    n = 0;
    const first = outbox.flushKey(null, [1, 2, 3], mint);
    const retry = outbox.flushKey(first, [1, 2, 3], mint);
    expect(retry.key).toBe(first.key);
    expect(retry).toBe(first);
  });

  it("is replaced when the batch has changed", () => {
    // Reusing a key for a batch that gained a keystroke is
    // `409 idempotency_key_reused`, and retrying that with the same key again
    // would stall the outbox for ever on its own header.
    n = 0;
    const first = outbox.flushKey(null, [1, 2], mint);
    const grown = outbox.flushKey(first, [1, 2, 3], mint);
    expect(grown.key).not.toBe(first.key);
  });

  it("treats a reordered batch as a different one", () => {
    n = 0;
    const first = outbox.flushKey(null, [1, 2], mint);
    expect(outbox.flushKey(first, [2, 1], mint).key).not.toBe(first.key);
  });
});
