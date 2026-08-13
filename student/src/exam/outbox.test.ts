import { describe, expect, it } from "vitest";

import { MAX_BATCH, batch, collapse, nextSeq, slotKey } from "./outbox";

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
