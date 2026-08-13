import { describe, expect, it } from "vitest";

import {
  attemptsLeft, deadlineText, limitText, ordered, stateOf, type Assignment,
} from "./assignments";

const NOW = Date.parse("2026-08-13T12:00:00Z");
const at = (offsetHours: number) =>
  new Date(NOW + offsetHours * 3_600_000).toISOString();

const make = (over: Partial<Assignment> = {}): Assignment => ({
  xid: "a1",
  opens_at: at(-24),
  closes_at: at(24),
  mode: "exam",
  max_attempts: 1,
  my_attempts_used: 0,
  ...over,
});

describe("what a student may do with assigned work", () => {
  it("is startable inside the window with an attempt left", () => {
    expect(stateOf(make(), NOW)).toBe("startable");
  });

  it("is upcoming before the window opens", () => {
    expect(stateOf(make({ opens_at: at(2), closes_at: at(48) }), NOW)).toBe("upcoming");
  });

  it("is closed after the window shuts", () => {
    expect(stateOf(make({ opens_at: at(-48), closes_at: at(-1) }), NOW)).toBe("closed");
  });

  it("is exhausted when every permitted attempt is spent", () => {
    expect(stateOf(make({ max_attempts: 2, my_attempts_used: 2 }), NOW)).toBe("exhausted");
  });

  it("says CLOSED rather than exhausted when it is both", () => {
    // Both are dead ends, but "closed" is the one a student can act on next
    // time; "you used your attempts" on work that also expired is misleading.
    const both = make({ closes_at: at(-1), max_attempts: 1, my_attempts_used: 1 });
    expect(stateOf(both, NOW)).toBe("closed");
  });

  it("treats a missing max_attempts as one", () => {
    const a = make({ my_attempts_used: 1 });
    delete (a as { max_attempts?: number }).max_attempts;
    expect(stateOf(a, NOW)).toBe("exhausted");
  });

  it("counts what is left, never below zero", () => {
    expect(attemptsLeft(make({ max_attempts: 3, my_attempts_used: 1 }))).toBe(2);
    expect(attemptsLeft(make({ max_attempts: 1, my_attempts_used: 5 }))).toBe(0);
  });
});

describe("the deadline, said coarsely", () => {
  it("counts down in minutes, hours and days", () => {
    expect(deadlineText(make({ closes_at: at(0.25) }), NOW)).toBe("closes in 15 min");
    expect(deadlineText(make({ closes_at: at(5) }), NOW)).toBe("closes in 5 hours");
    expect(deadlineText(make({ closes_at: at(72) }), NOW)).toBe("closes in 3 days");
  });

  it("singularises one hour and one day", () => {
    expect(deadlineText(make({ closes_at: at(1) }), NOW)).toBe("closes in 1 hour");
    expect(deadlineText(make({ closes_at: at(24) }), NOW)).toBe("closes in 1 day");
  });

  it("says OPENS for work that has not started", () => {
    expect(deadlineText(make({ opens_at: at(3), closes_at: at(48) }), NOW))
      .toBe("opens in 3 hours");
  });

  it("says closed once it is over", () => {
    expect(deadlineText(make({ opens_at: at(-48), closes_at: at(-1) }), NOW)).toBe("closed");
  });
});

describe("the time limit", () => {
  it("reads in minutes", () => {
    expect(limitText(make({ time_limit_seconds: 3600 }))).toBe("60 min");
  });

  it("is null when the assignment is untimed", () => {
    expect(limitText(make({ time_limit_seconds: null }))).toBeNull();
    expect(limitText(make({ time_limit_seconds: 0 }))).toBeNull();
  });
});

describe("the order work is shown in", () => {
  it("puts what can be done first, and what is dead last", () => {
    const list: Assignment[] = [
      make({ xid: "closed", opens_at: at(-72), closes_at: at(-1) }),
      make({ xid: "upcoming", opens_at: at(5), closes_at: at(72) }),
      make({ xid: "startable" }),
      make({ xid: "exhausted", max_attempts: 1, my_attempts_used: 1 }),
    ];
    expect(ordered(list, NOW).map((a) => a.xid))
      .toEqual(["startable", "upcoming", "exhausted", "closed"]);
  });

  it("puts the soonest-closing open work first — it is the one at risk", () => {
    const list: Assignment[] = [
      make({ xid: "later", closes_at: at(48) }),
      make({ xid: "sooner", closes_at: at(3) }),
    ];
    expect(ordered(list, NOW).map((a) => a.xid)).toEqual(["sooner", "later"]);
  });

  it("puts the soonest-opening upcoming work first", () => {
    const list: Assignment[] = [
      make({ xid: "later", opens_at: at(48), closes_at: at(96) }),
      make({ xid: "sooner", opens_at: at(6), closes_at: at(96) }),
    ];
    expect(ordered(list, NOW).map((a) => a.xid)).toEqual(["sooner", "later"]);
  });

  it("does not mutate the array it was given", () => {
    const list = [make({ xid: "b", closes_at: at(48) }), make({ xid: "a", closes_at: at(3) })];
    const before = list.map((a) => a.xid);
    ordered(list, NOW);
    expect(list.map((a) => a.xid)).toEqual(before);
  });
});
