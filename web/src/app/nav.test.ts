import { describe, expect, it } from "vitest";

import { ACCOUNT, NAV, activeFor, groupKeyFor, groupsFor } from "./nav";

describe("the sidebar covers the product", () => {
  it("has no duplicate destinations", () => {
    const paths = NAV.flatMap((g) => g.items.map((i) => i.to));
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("leads the library with the three skills that exist", () => {
    const library = NAV.find((g) => g.key === "library")!;
    expect(library.items.slice(0, 3).map((i) => i.label))
      .toEqual(["Reading", "Listening", "Speaking"]);
  });

  it("does NOT offer Writing, which has no screens and no engine", () => {
    // ADR-0001 Assumption 5. A nav entry to an empty page is a promise the
    // product does not keep; it appears when the screens do.
    const labels = NAV.flatMap((g) => g.items.map((i) => i.label));
    expect(labels).not.toContain("Writing");
  });

  it("keeps every group small enough to scan", () => {
    // The old nav hid 26 pages behind 5 tabs. Groups that grow past about seven
    // are the same failure returning in a different shape.
    for (const group of NAV) expect(group.items.length).toBeLessThanOrEqual(7);
  });
});

describe("what a centre teacher sees", () => {
  it("hides the platform group from anyone who is not a platform admin", () => {
    const keys = groupsFor(false).map((g) => g.key);
    expect(keys).not.toContain("platform");
  });

  it("shows it to a platform admin", () => {
    expect(groupsFor(true).map((g) => g.key)).toContain("platform");
  });

  it("removes six items from a teacher's sidebar, not one or two", () => {
    const teacher = groupsFor(false).flatMap((g) => g.items).length;
    const admin = groupsFor(true).flatMap((g) => g.items).length;
    expect(admin - teacher).toBe(6);
  });
});

describe("which item is current", () => {
  it("matches an exact path", () => {
    expect(activeFor("/billing")).toBe("/billing");
  });

  it("keeps the parent lit on a detail route", () => {
    expect(activeFor("/tests/abc-123")).toBe("/tests");
  });

  it("keeps Tests lit while composing a version", () => {
    // Without this the sidebar goes dim exactly when somebody has navigated
    // deepest into the product.
    expect(activeFor("/versions/abc-123")).toBe("/tests");
    expect(activeFor("/versions/abc-123/preview")).toBe("/tests");
  });

  it("keeps Account lit on the invites screen", () => {
    expect(activeFor("/invites")).toBe(ACCOUNT.to);
  });

  it("prefers the longest match, so a shorter prefix cannot steal it", () => {
    expect(activeFor("/item-analysis")).toBe("/item-analysis");
  });

  it("returns nothing for a path the sidebar does not own", () => {
    expect(activeFor("/nowhere")).toBeUndefined();
  });
});

describe("which group the accordion should open", () => {
  it("names the group holding the current page", () => {
    expect(groupKeyFor("/passages")).toBe("library");
    expect(groupKeyFor("/billing")).toBe("centre");
    expect(groupKeyFor("/item-analysis")).toBe("reports");
  });

  it("follows a nested route to its parent's group", () => {
    // Landing deep in a version must still open Library, or the sidebar shows
    // you a different part of the product than the one you are in.
    expect(groupKeyFor("/versions/abc-123/preview")).toBe("library");
  });

  it("returns nothing for Account, which sits outside the groups", () => {
    expect(groupKeyFor("/account")).toBeUndefined();
  });

  it("returns nothing for a path the sidebar does not own", () => {
    expect(groupKeyFor("/nowhere")).toBeUndefined();
  });

  it("names a group for EVERY item, so no page can open nothing", () => {
    for (const group of NAV) {
      for (const item of group.items) {
        expect(groupKeyFor(item.to)).toBe(group.key);
      }
    }
  });
});
