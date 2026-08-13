import { describe, expect, it } from "vitest";

import { add, removeAt, segments, spanAt, type Span } from "./highlights";

const TEXT = "The quick brown fox jumps over the lazy dog.";

describe("adding a highlight", () => {
  it("keeps a single span", () => {
    expect(add([], { start: 4, end: 9 })).toEqual([{ start: 4, end: 9 }]);
  });

  it("normalises a backwards selection — dragging right-to-left is normal", () => {
    expect(add([], { start: 9, end: 4 })).toEqual([{ start: 4, end: 9 }]);
  });

  it("discards an empty selection, which is what a plain click produces", () => {
    expect(add([], { start: 7, end: 7 })).toEqual([]);
  });

  it("merges an overlapping span rather than stacking two", () => {
    expect(add([{ start: 4, end: 9 }], { start: 6, end: 15 }))
      .toEqual([{ start: 4, end: 15 }]);
  });

  it("merges spans that merely TOUCH", () => {
    // Highlighting a word and then the word after it must leave one span. Two
    // abutting spans look identical on screen and then "clear" removes only half.
    expect(add([{ start: 0, end: 3 }], { start: 3, end: 9 }))
      .toEqual([{ start: 0, end: 9 }]);
  });

  it("swallows a span entirely contained by the new one", () => {
    expect(add([{ start: 5, end: 7 }], { start: 0, end: 20 }))
      .toEqual([{ start: 0, end: 20 }]);
  });

  it("keeps disjoint spans apart and sorted", () => {
    const spans = add(add([], { start: 20, end: 25 }), { start: 4, end: 9 });
    expect(spans).toEqual([{ start: 4, end: 9 }, { start: 20, end: 25 }]);
  });
});

describe("clearing", () => {
  const spans: Span[] = [{ start: 4, end: 9 }, { start: 20, end: 25 }];

  it("removes the whole span the offset falls in, never splitting it", () => {
    expect(removeAt(spans, 6)).toEqual([{ start: 20, end: 25 }]);
  });

  it("leaves everything alone when the offset is outside every span", () => {
    expect(removeAt(spans, 15)).toEqual(spans);
  });

  it("treats the end offset as outside — spans are half-open", () => {
    expect(removeAt(spans, 9)).toEqual(spans);
    expect(removeAt(spans, 4)).toEqual([{ start: 20, end: 25 }]);
  });
});

describe("finding the span under a click", () => {
  it("returns it, or nothing", () => {
    const spans: Span[] = [{ start: 4, end: 9 }];
    expect(spanAt(spans, 5)).toEqual({ start: 4, end: 9 });
    expect(spanAt(spans, 30)).toBeUndefined();
  });
});

describe("rendering into segments", () => {
  it("returns the whole text as one plain run when nothing is highlighted", () => {
    expect(segments(TEXT, [])).toEqual([{ text: TEXT, highlighted: false, start: 0 }]);
  });

  it("splits into plain, highlighted, plain", () => {
    const out = segments(TEXT, [{ start: 4, end: 9 }]);
    expect(out.map((s) => s.text)).toEqual(["The ", "quick", " brown fox jumps over the lazy dog."]);
    expect(out.map((s) => s.highlighted)).toEqual([false, true, false]);
  });

  it("gives every segment its own start offset, so a click maps back to text", () => {
    const out = segments(TEXT, [{ start: 4, end: 9 }]);
    expect(out.map((s) => s.start)).toEqual([0, 4, 9]);
  });

  it("reassembles to exactly the original text", () => {
    const out = segments(TEXT, [{ start: 4, end: 9 }, { start: 20, end: 25 }]);
    expect(out.map((s) => s.text).join("")).toBe(TEXT);
  });

  it("handles a highlight running to the very end without a trailing empty run", () => {
    const out = segments("abc", [{ start: 1, end: 3 }]);
    expect(out).toEqual([
      { text: "a", highlighted: false, start: 0 },
      { text: "bc", highlighted: true, start: 1 },
    ]);
  });

  it("clamps a span that runs past the end of the text", () => {
    const out = segments("abc", [{ start: 1, end: 99 }]);
    expect(out.map((s) => s.text).join("")).toBe("abc");
    expect(out[1]!.text).toBe("bc");
  });

  it("always returns at least one segment, so there is a node to select in", () => {
    expect(segments("", [])).toHaveLength(1);
  });
});
