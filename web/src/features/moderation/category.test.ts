/**
 * The safety queue's category column.
 *
 * Seven of the eight values are single words. The eighth is `sexual_content`,
 * and it reached the screen with the underscore in it — database text in the
 * column a moderator triages by, one row above `grooming` and `harassment` in
 * plain English.
 */

import { describe, expect, it } from "vitest";

import { categoryLabel } from "./queue";

describe("categoryLabel", () => {
  it("turns the one multi-word value into English", () => {
    expect(categoryLabel("sexual_content")).toBe("Sexual content");
  });

  it("capitalises the single-word ones without otherwise touching them", () => {
    expect(["harassment", "grooming", "hate", "violence", "spam", "cheating", "other"]
      .map(categoryLabel))
      .toEqual(["Harassment", "Grooming", "Hate", "Violence", "Spam", "Cheating", "Other"]);
  });

  it("renders an unknown value as itself rather than dropping it", () => {
    // The enum can grow server-side without this console being redeployed. A
    // category nobody can read is bad; a report whose category vanished is
    // worse — it is the field a moderator sorts the queue by.
    expect(categoryLabel("self_harm")).toBe("Self harm");
    expect(categoryLabel("")).toBe("");
  });

  it("renders nothing for a missing category", () => {
    // The generated type has the field optional, and the cell this replaced
    // rendered nothing rather than "undefined".
    expect(categoryLabel(undefined)).toBe("");
  });
});
