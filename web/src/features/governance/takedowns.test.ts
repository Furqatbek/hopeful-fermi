import { describe, expect, it } from "vitest";

import { DECISION_LIST, isOpen, waitingDays } from "./takedowns";

describe("DECISION_LIST", () => {
  it("offers the five the server accepts and not `received`", () => {
    // The PATCH pattern is `reviewing|upheld|rejected|counter_noticed|withdrawn`.
    // Offering `received` would be a control whose only outcome is a 422.
    expect(DECISION_LIST.map((d) => d.value)).toEqual([
      "reviewing", "upheld", "rejected", "counter_noticed", "withdrawn",
    ]);
  });

  it("says what each one means, because four of them are final", () => {
    expect(DECISION_LIST.every((d) => d.help.length > 0)).toBe(true);
  });
});

describe("isOpen", () => {
  it("is the queue's own definition: received or reviewing", () => {
    expect(isOpen("received")).toBe(true);
    expect(isOpen("reviewing")).toBe(true);
  });

  it("keeps `reviewing` in the queue", () => {
    // `reviewing` is a decision that does NOT close the request. Treating every
    // decision as final would tell an admin a request had left a queue it is
    // still sitting in.
    expect(isOpen("reviewing")).toBe(true);
    expect(isOpen("upheld")).toBe(false);
    expect(isOpen("rejected")).toBe(false);
    expect(isOpen("counter_noticed")).toBe(false);
    expect(isOpen("withdrawn")).toBe(false);
  });

  it("is false for a status that is missing", () => {
    expect(isOpen(undefined)).toBe(false);
    expect(isOpen(null)).toBe(false);
  });
});

describe("waitingDays", () => {
  const now = new Date("2026-08-03T09:00:00Z");

  it("counts whole days since filing", () => {
    expect(waitingDays("2026-07-14T09:00:00Z", now)).toBe(20);
    expect(waitingDays("2026-08-02T09:00:00Z", now)).toBe(1);
  });

  it("is zero for something filed today", () => {
    expect(waitingDays("2026-08-03T02:00:00Z", now)).toBe(0);
  });

  it("does not go negative on a clock difference", () => {
    expect(waitingDays("2026-08-03T09:05:00Z", now)).toBe(0);
  });

  it("is null, not zero, when the date is missing or unreadable", () => {
    // Zero is a real answer here — filed today — so using it for "we do not
    // know" would show the oldest possible complaint as the freshest.
    expect(waitingDays(undefined, now)).toBeNull();
    expect(waitingDays(null, now)).toBeNull();
    expect(waitingDays("not a date", now)).toBeNull();
  });
});
