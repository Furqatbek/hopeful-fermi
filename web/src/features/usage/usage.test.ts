import { describe, expect, it } from "vitest";

import { byRisk, tally, type UsageReference } from "./usage";

const ref = (title: string, status: string): UsageReference =>
  ({ kind: "test_version", xid: `x-${title}`, title, status });

describe("tally", () => {
  it("separates archived from draft, which the server's two counts do not", () => {
    // `_usage()` partitions on `status == "published"` and calls the rest drafts,
    // so this report arrives as published_count 1, draft_count 2. Showing "2
    // drafts" would send an author looking for a paper still being written.
    const counted = tally({
      published_count: 1,
      draft_count: 2,
      references: [ref("Live", "published"), ref("Next term", "draft"),
                   ref("Retired", "archived")],
    });
    expect(counted).toEqual({ published: 1, draft: 1, archived: 1, other: 0, total: 3 });
  });

  it("counts a status it does not recognise rather than dropping it", () => {
    // A row silently missing from the total is the one an author does not check.
    const counted = tally({ references: [ref("Under review", "in_review")] });
    expect(counted.other).toBe(1);
    expect(counted.total).toBe(1);
  });

  it("falls back to the server's numbers when the list is absent", () => {
    expect(tally({ published_count: 4, draft_count: 1 }))
      .toEqual({ published: 4, draft: 1, archived: 0, other: 0, total: 5 });
  });

  it("is all zeroes while the report is still loading", () => {
    expect(tally(undefined).total).toBe(0);
  });

  it("reports nothing in use as nothing, not as unknown", () => {
    expect(tally({ published_count: 0, draft_count: 0, references: [] }).total).toBe(0);
  });
});

describe("byRisk", () => {
  it("puts the published papers first and the archived ones last", () => {
    const sorted = byRisk([ref("Retired", "archived"), ref("Next term", "draft"),
                           ref("Live", "published")]);
    expect(sorted.map((r) => r.title)).toEqual(["Live", "Next term", "Retired"]);
  });

  it("keeps a stable order within one status", () => {
    const sorted = byRisk([ref("Beta", "draft"), ref("Alpha", "draft")]);
    expect(sorted.map((r) => r.title)).toEqual(["Alpha", "Beta"]);
  });

  it("sorts an unknown status after the ones it knows, still listed", () => {
    const sorted = byRisk([ref("Mystery", "sideways"), ref("Retired", "archived")]);
    expect(sorted.map((r) => r.title)).toEqual(["Retired", "Mystery"]);
  });

  it("does not mutate the array it was given", () => {
    const rows = [ref("Retired", "archived"), ref("Live", "published")];
    byRisk(rows);
    expect(rows.map((r) => r.title)).toEqual(["Retired", "Live"]);
  });

  it("is empty when nothing uses the asset", () => {
    expect(byRisk(undefined)).toEqual([]);
  });
});
