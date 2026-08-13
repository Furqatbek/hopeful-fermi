import { describe, expect, it } from "vitest";

import { asList, toggleSelection } from "./Question";
import { answered } from "./Runner";

describe("reading a slot value that may be a list", () => {
  it("passes a list through", () => {
    expect(asList(["B", "D"])).toEqual(["B", "D"]);
  });

  it("treats a lone string as a selection of one", () => {
    // A resumed attempt can hand back either shape; neither may crash a render.
    expect(asList("B")).toEqual(["B"]);
  });

  it("is empty for nothing and for the empty string", () => {
    expect(asList(undefined)).toEqual([]);
    expect(asList("")).toEqual([]);
  });
});

describe("toggling a selection", () => {
  const OPTIONS = ["A", "B", "C", "D", "E"];

  it("adds a choice", () => {
    expect(toggleSelection([], "C", OPTIONS)).toEqual(["C"]);
  });

  it("removes one already chosen", () => {
    expect(toggleSelection(["B", "D"], "B", OPTIONS)).toEqual(["D"]);
  });

  it("keeps the options' order, not the click order", () => {
    // Clicking D then B must still read B, D — it is the order the answer is
    // shown back in, and a reordered pair looks like a different answer.
    const afterD = toggleSelection([], "D", OPTIONS);
    expect(toggleSelection(afterD, "B", OPTIONS)).toEqual(["B", "D"]);
  });

  it("does NOT cap the selection", () => {
    // Over-selection scores zero and the student is warned, not blocked: the
    // rule they have to learn is "TWO means two", and hiding the breach here
    // teaches it in the real test instead.
    expect(toggleSelection(["B", "D"], "E", OPTIONS)).toEqual(["B", "D", "E"]);
  });

  it("ignores an id that is not an option", () => {
    expect(toggleSelection([], "Z", OPTIONS)).toEqual([]);
  });
});

describe("whether a slot counts as answered, for the palette", () => {
  it("an empty selection is NOT answered", () => {
    // `[] !== ""` is true, so a string-only test lit the marker for every
    // untouched multi-select on the paper.
    expect(answered([])).toBe(false);
  });

  it("a selection of one or more is answered", () => {
    expect(answered(["B"])).toBe(true);
  });

  it("keeps the old behaviour for text", () => {
    expect(answered("")).toBe(false);
    expect(answered(undefined)).toBe(false);
    expect(answered("430")).toBe(true);
  });
});
