import { describe, expect, it } from "vitest";

import {
  ageBandLabel,
  appearsInMyList,
  rangeLabel,
  slotProblems,
  type Creator,
  type SlotDraft,
} from "./slotRules";

const TEACHER: Creator = { isPlatformAdmin: false, hasOrg: true, isMinor: false };

function future(hours = 24): string {
  return new Date(Date.now() + hours * 3600_000).toISOString().slice(0, 16);
}

function draft(over: Partial<SlotDraft> = {}): SlotDraft {
  return {
    startsAt: future(), ageBand: "adult", audience: "public",
    cohortXid: "", bandMin: "", bandMax: "", ...over,
  };
}

describe("slotProblems", () => {
  it("passes an ordinary public adult slot", () => {
    expect(slotProblems(draft(), TEACHER)).toEqual([]);
  });

  it("refuses until an age group is chosen", () => {
    // The whole point of the control: age band decides who may ever enter, and
    // a default is a choice nobody made.
    expect(slotProblems(draft({ ageBand: "" }), TEACHER)).toHaveLength(1);
  });

  it("refuses a mixed-age session that is not a class session", () => {
    // The server answers 409 mixed_requires_cohort.
    const problems = slotProblems(
      draft({ ageBand: "mixed_supervised", audience: "public" }), TEACHER);
    expect(problems.some((p) => p.includes("class session"))).toBe(true);
  });

  it("accepts a mixed-age class session with a class chosen", () => {
    expect(slotProblems(
      draft({ ageBand: "mixed_supervised", audience: "cohort", cohortXid: "c1" }),
      TEACHER)).toEqual([]);
  });

  it("refuses a class session with no class", () => {
    expect(slotProblems(draft({ audience: "cohort" }), TEACHER)).toHaveLength(1);
  });

  it("refuses an inverted band range", () => {
    // 409 invalid_band_range: now that the range filters, an inverted one is a
    // slot nobody can book.
    const problems = slotProblems(draft({ bandMin: "7", bandMax: "5" }), TEACHER);
    expect(problems.some((p) => p.includes("low to high"))).toBe(true);
  });

  it("refuses a band off the 0-9 scale", () => {
    expect(slotProblems(draft({ bandMin: "12" }), TEACHER)).toHaveLength(1);
    expect(slotProblems(draft({ bandMax: "-1" }), TEACHER)).toHaveLength(1);
  });

  it("accepts a one-ended range and no range at all", () => {
    expect(slotProblems(draft({ bandMin: "5.5" }), TEACHER)).toEqual([]);
    expect(slotProblems(draft({ bandMax: "6" }), TEACHER)).toEqual([]);
  });

  it("refuses a start time that has passed", () => {
    // Accepted by the server with 201 and then listed for nobody, because
    // list_slots filters starts_at >= now.
    const problems = slotProblems(
      draft({ startsAt: new Date(Date.now() - 3600_000).toISOString().slice(0, 16) }),
      TEACHER);
    expect(problems.some((p) => p.includes("past"))).toBe(true);
  });

  it("refuses a centre session from an account with no centre", () => {
    // Measured: a platform admin's `org` slot stores org_id NULL and matches
    // `s.org_id = ANY(:orgs)` for no one.
    const platform: Creator = { isPlatformAdmin: true, hasOrg: false, isMinor: false };
    const problems = slotProblems(draft({ audience: "org" }), platform);
    expect(problems.some((p) => p.includes("shown to nobody"))).toBe(true);
    expect(slotProblems(draft({ audience: "public" }), platform)).toEqual([]);
  });

  it("reports every problem at once, not the first", () => {
    expect(slotProblems(
      draft({ ageBand: "", audience: "cohort", bandMin: "7", bandMax: "5" }),
      TEACHER).length).toBeGreaterThan(2);
  });
});

describe("appearsInMyList", () => {
  it("says yes for a public slot in the creator's own age band", () => {
    expect(appearsInMyList({ audience: "public", age_band: "adult" }, TEACHER).shown)
      .toBe(true);
  });

  it("says no for a class session, because the list is scoped to class members", () => {
    const answer = appearsInMyList({ audience: "cohort", age_band: "mixed_supervised" },
                                   TEACHER);
    expect(answer.shown).toBe(false);
    expect(answer.reason).toContain("class roster");
  });

  it("says no when an adult opens a session for minors", () => {
    const answer = appearsInMyList({ audience: "org", age_band: "minor" }, TEACHER);
    expect(answer.shown).toBe(false);
    expect(answer.reason).toContain("age group");
  });

  it("gives the age answer before the class answer", () => {
    // A minor slot for a class the teacher is not in fails both ways; the age
    // filter is the one that would still hide it if they joined the class.
    expect(appearsInMyList({ audience: "cohort", age_band: "minor" }, TEACHER).reason)
      .toContain("age group");
  });
});

describe("labels", () => {
  it("spells out each age band", () => {
    expect(ageBandLabel("minor")).toBe("Under 18 only");
    expect(ageBandLabel("adult")).toBe("18 and over only");
    expect(ageBandLabel("mixed_supervised")).toContain("supervised");
  });

  it("reads a range the way the server's refusal does", () => {
    expect(rangeLabel(5, 6.5)).toBe("Band 5-6.5");
    expect(rangeLabel(5, null)).toBe("Band 5 and above");
    expect(rangeLabel(null, 6)).toBe("Band 6 and below");
    expect(rangeLabel(null, null)).toBe("Any band");
  });
});
