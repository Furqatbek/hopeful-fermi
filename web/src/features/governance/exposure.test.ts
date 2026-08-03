import { describe, expect, it } from "vitest";

import { burnPercent, rankByBurn } from "./exposure";

const item = (xid: string, burn: number | null, timesSat = 0) => ({
  xid,
  exposure: burn === null ? null : { burn_score: burn, times_sat: timesSat },
});

const order = (rows: ReturnType<typeof item>[]) =>
  rankByBurn(rows).map((row) => row.xid);

describe("burnPercent", () => {
  it("reads as a percentage", () => {
    expect(burnPercent(0.734)).toBe(73);
    expect(burnPercent(0)).toBe(0);
    expect(burnPercent(1)).toBe(100);
  });

  it("clamps rather than drawing a bar out of its row", () => {
    expect(burnPercent(1.4)).toBe(100);
    expect(burnPercent(-0.2)).toBe(0);
  });

  it("is zero for a score that is not a number", () => {
    expect(burnPercent(undefined)).toBe(0);
    expect(burnPercent(null)).toBe(0);
    expect(burnPercent(Number.NaN)).toBe(0);
  });
});

describe("rankByBurn", () => {
  it("puts the burning item at the top of the page", () => {
    expect(order([item("fresh", 0.02), item("gone", 0.91), item("watch", 0.44)]))
      .toEqual(["gone", "watch", "fresh"]);
  });

  it("does not treat an item it could not read as fresh", () => {
    // The failure: an exposure request that has not answered sorts as zero,
    // lands among the safe items, and the screen implies a fact it does not
    // have on the one question it exists to answer.
    expect(order([item("unknown", null), item("quiet", 0.05)]))
      .toEqual(["quiet", "unknown"]);
  });

  it("breaks a tie on sittings, because burn saturates", () => {
    expect(order([item("a", 0.8, 400), item("b", 0.8, 1900)]))
      .toEqual(["b", "a"]);
  });

  it("leaves the input alone", () => {
    const rows = [item("fresh", 0.02), item("gone", 0.91)];
    rankByBurn(rows);
    expect(rows.map((row) => row.xid)).toEqual(["fresh", "gone"]);
  });

  it("keeps the bank's order among items it cannot tell apart", () => {
    expect(order([item("first", null), item("second", null)]))
      .toEqual(["first", "second"]);
  });
});
