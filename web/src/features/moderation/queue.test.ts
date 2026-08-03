import { describe, expect, it } from "vitest";

import { forReview, isUnattended, priorityRank, unattendedCount, waited } from "./queue";
import type { Reviewable } from "./queue";

const report = (priority: string, created: string, status = "new") =>
  ({ priority, created_at: created, status, xid: `${priority}-${created}` });

describe("priorityRank", () => {
  it("puts critical above high above normal", () => {
    expect(priorityRank("critical")).toBeLessThan(priorityRank("high"));
    expect(priorityRank("high")).toBeLessThan(priorityRank("normal"));
  });

  it("sorts an unrecognised priority last, not first", () => {
    // A priority this console has never heard of must not jump the queue ahead
    // of a grooming report because it happened to sort earlier.
    expect(priorityRank("mauve")).toBeGreaterThan(priorityRank("normal"));
    expect(priorityRank(undefined)).toBeGreaterThan(priorityRank("normal"));
  });
});

describe("forReview", () => {
  it("undoes the order the server sends", () => {
    // `ORDER BY priority DESC` on a text column is alphabetical: PostgreSQL
    // returns normal, high, critical. Left alone, the reports that involve a
    // minor and grooming are the last rows on the page.
    const fromServer = [
      report("normal", "2026-08-01T09:00:00Z"),
      report("high", "2026-08-01T09:00:00Z"),
      report("critical", "2026-08-01T09:00:00Z"),
    ];
    expect(forReview(fromServer).map((r) => r.priority)).toEqual(
      ["critical", "high", "normal"]);
  });

  it("takes the longest wait first within a priority", () => {
    const rows = [
      report("critical", "2026-08-01T12:00:00Z"),
      report("critical", "2026-08-01T08:00:00Z"),
      report("critical", "2026-08-01T10:00:00Z"),
    ];
    expect(forReview(rows).map((r) => r.created_at)).toEqual([
      "2026-08-01T08:00:00Z",
      "2026-08-01T10:00:00Z",
      "2026-08-01T12:00:00Z",
    ]);
  });

  it("never lets age outrank priority", () => {
    // A week-old spam report does not come before a grooming report filed a
    // minute ago.
    const rows = [
      report("critical", "2026-08-01T12:00:00Z"),
      report("normal", "2026-07-25T08:00:00Z"),
    ];
    expect(forReview(rows)[0]?.priority).toBe("critical");
  });

  it("does not reorder the caller's array", () => {
    // It is the query cache's array; sorting it in place changes what every
    // other reader of that cache entry sees.
    const rows = [
      report("normal", "2026-08-01T09:00:00Z"),
      report("critical", "2026-08-01T09:00:00Z"),
    ];
    forReview(rows);
    expect(rows[0]?.priority).toBe("normal");
  });

  it("keeps a report with no timestamp instead of dropping it", () => {
    const rows: Reviewable[] = [
      { priority: "high", status: "new" },
      report("high", "2026-08-01T09:00:00Z"),
    ];
    expect(forReview(rows)).toHaveLength(2);
    // Behind the dated ones, because an undated row has no claim to being the
    // longest waiting — sorting it as 1970 would put it at the very top.
    expect(forReview(rows).at(-1)?.created_at).toBeUndefined();
  });

  it("is empty for an empty queue", () => {
    expect(forReview([])).toEqual([]);
  });
});

describe("isUnattended", () => {
  it("counts every status before a decision", () => {
    expect(isUnattended({ status: "new" })).toBe(true);
    expect(isUnattended({ status: "triage" })).toBe(true);
    expect(isUnattended({ status: "investigating" })).toBe(true);
  });

  it("does not count a finished one", () => {
    expect(isUnattended({ status: "actioned" })).toBe(false);
    expect(isUnattended({ status: "dismissed" })).toBe(false);
  });
});

describe("unattendedCount", () => {
  it("is the number the minors banner leads with", () => {
    expect(unattendedCount([
      report("critical", "2026-08-01T09:00:00Z", "new"),
      report("high", "2026-08-01T09:00:00Z", "actioned"),
      report("high", "2026-08-01T09:00:00Z", "triage"),
    ])).toBe(2);
  });

  it("is zero, not a false alarm, when everything is handled", () => {
    expect(unattendedCount([report("critical", "2026-08-01T09:00:00Z", "actioned")]))
      .toBe(0);
  });
});

describe("waited", () => {
  const now = Date.parse("2026-08-03T12:00:00Z");

  it("reads in the units a moderator thinks in", () => {
    expect(waited("2026-08-03T11:30:00Z", now)).toBe("30 min");
    expect(waited("2026-08-03T06:00:00Z", now)).toBe("6 h");
    expect(waited("2026-07-30T12:00:00Z", now)).toBe("4 days");
  });

  it("does not read as the future when the clocks disagree", () => {
    // The browser's clock is not the server's. A few seconds of skew must not
    // render as a negative age.
    expect(waited("2026-08-03T12:00:20Z", now)).toBe("just now");
  });

  it("says so rather than guessing when there is no timestamp", () => {
    expect(waited(undefined, now)).toBe("unknown");
    expect(waited("not a date", now)).toBe("unknown");
  });
});
