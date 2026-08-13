import { describe, expect, it } from "vitest";

import { byPart, marker, step, unanswered, type Slot } from "./palette";

const slot = (over: Partial<Slot> = {}): Slot => ({
  number: 1, part: 1, answered: false, flagged: false, ...over,
});

describe("the marker follows the real client's square-to-circle language", () => {
  it("is a square when not flagged", () => {
    expect(marker(slot(), 1).shape).toBe("square");
  });

  it("becomes a CIRCLE when flagged for review", () => {
    // The verified behaviour: clicking Review "changes the question number from
    // a square to a circle". Not a colour, not an icon — the shape.
    expect(marker(slot({ flagged: true }), 1).shape).toBe("circle");
  });

  it("keeps the circle when a flagged question is also answered", () => {
    const m = marker(slot({ flagged: true, answered: true }), 99);
    expect(m.shape).toBe("circle");
    expect(m.answered).toBe(true);
  });

  it("marks answered separately from shape — it renders as an underline", () => {
    // "a line appears beneath the question once it is answered" — the official
    // wording. Filling the marker was a guess, and it was the wrong one.
    expect(marker(slot({ answered: true }), 99).answered).toBe(true);
    expect(marker(slot({ answered: false }), 99).answered).toBe(false);
  });

  it("marks only the question actually being viewed as current", () => {
    expect(marker(slot({ number: 7 }), 7).current).toBe(true);
    expect(marker(slot({ number: 7 }), 8).current).toBe(false);
  });
});

describe("grouping into parts", () => {
  it("preserves order and keeps each part together", () => {
    const groups = byPart([
      slot({ number: 1, part: 1 }), slot({ number: 2, part: 1 }),
      slot({ number: 3, part: 2 }),
    ]);
    expect(groups.map((g) => g.part)).toEqual([1, 2]);
    expect(groups[0]!.slots.map((s) => s.number)).toEqual([1, 2]);
  });

  it("handles an empty section without inventing a group", () => {
    expect(byPart([])).toEqual([]);
  });
});

describe("counting what is left", () => {
  it("counts the blanks, not the answers", () => {
    expect(unanswered([slot({ answered: true }), slot(), slot()])).toBe(2);
  });
});

describe("stepping between questions", () => {
  const slots = [slot({ number: 1 }), slot({ number: 2 }), slot({ number: 3 })];

  it("moves one either way", () => {
    expect(step(slots, 2, 1)).toBe(3);
    expect(step(slots, 2, -1)).toBe(1);
  });

  it("CLAMPS at both ends rather than wrapping", () => {
    // Wrapping from the last question back to the first is the convenience that
    // silently throws a student back to question 1 near the end of a section.
    expect(step(slots, 3, 1)).toBe(3);
    expect(step(slots, 1, -1)).toBe(1);
  });

  it("leaves an unknown current question alone", () => {
    expect(step(slots, 99, 1)).toBe(99);
  });
});
