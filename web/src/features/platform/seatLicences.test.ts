import { describe, expect, it } from "vitest";

import { hasLiveSeatLicence, quantityLabel, shadowedFeatures } from "./seatLicences";

const seat = (feature: string, revoked: string | null = null) =>
  ({ feature, source_kind: "seat", revoked_at: revoked });

describe("shadowedFeatures", () => {
  it("says nothing about a single licence", () => {
    expect(shadowedFeatures([seat("mock.unlimited")])).toEqual([]);
  });

  it("names the feature when two live licences compete", () => {
    // The bug this exists for. Granting twice takes two clicks, and only the
    // newest is in force — the other grants nothing, while reading as capacity.
    expect(shadowedFeatures([seat("mock.unlimited"), seat("mock.unlimited")]))
      .toEqual(["mock.unlimited"]);
  });

  it("does not count a revoked licence as competition", () => {
    expect(shadowedFeatures([
      seat("mock.unlimited"),
      seat("mock.unlimited", "2026-01-01T00:00:00Z"),
    ])).toEqual([]);
  });

  it("treats two features as two licences, not a clash", () => {
    expect(shadowedFeatures([seat("mock.unlimited"), seat("competition.entry")]))
      .toEqual([]);
  });

  it("ignores rows that are not seat licences", () => {
    // An org-wide grant and a seat licence for the same feature are not two
    // seat licences; only one of them is resolved by the seat lookup at all.
    expect(shadowedFeatures([
      seat("mock.unlimited"),
      { feature: "mock.unlimited", source_kind: "trial", revoked_at: null },
      { feature: "mock.unlimited", source_kind: "order", revoked_at: null },
    ])).toEqual([]);
  });

  it("names each clashing feature once, not once per row", () => {
    expect(shadowedFeatures([
      seat("mock.unlimited"), seat("mock.unlimited"), seat("mock.unlimited"),
    ])).toEqual(["mock.unlimited"]);
  });
});

describe("hasLiveSeatLicence", () => {
  it("is false for a centre holding only org-wide grants", () => {
    expect(hasLiveSeatLicence([
      { feature: "org.assignments", source_kind: "trial", revoked_at: null },
    ])).toBe(false);
  });

  it("is false once the only seat licence is revoked", () => {
    expect(hasLiveSeatLicence([seat("mock.unlimited", "2026-01-01T00:00:00Z")]))
      .toBe(false);
  });

  it("is true for a live one", () => {
    expect(hasLiveSeatLicence([seat("mock.unlimited")])).toBe(true);
  });
});

describe("quantityLabel", () => {
  it("reads a seat licence as seats, never as a balance", () => {
    // "3 of 3" was the bug: nothing consumes a seat, so it said that whether
    // every seat was free or every seat was taken.
    expect(quantityLabel({ source_kind: "seat", quantity: 3, remaining: 3 }))
      .toBe("3 seats");
  });

  it("does not pluralise one seat", () => {
    expect(quantityLabel({ source_kind: "seat", quantity: 1, remaining: 1 }))
      .toBe("1 seat");
  });

  it("still reports a real consumable balance", () => {
    expect(quantityLabel({ source_kind: "order", quantity: 4, remaining: 1 }))
      .toBe("1 of 4");
  });

  it("calls a null quantity unlimited", () => {
    expect(quantityLabel({ source_kind: "trial", quantity: null })).toBe("unlimited");
  });

  it("does not call a seat licence with no count unlimited", () => {
    // The server refuses to create one, but the panel must not invent capacity
    // for a row that somehow exists — nought is the honest answer.
    expect(quantityLabel({ source_kind: "seat", quantity: null })).toBe("0 seats");
  });
});
