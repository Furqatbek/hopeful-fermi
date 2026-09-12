import { describe, expect, it } from "vitest";

import { paragraphSlots, rubricOf } from "./Question";

describe("the paragraph slots of a matching_headings payload", () => {
  const SLOT_KEYS = ["s1", "s2", "s3"];

  it("returns every paragraph, in payload order, with distinct keys", () => {
    // The registry allows up to fourteen; the renderer used to bind ONE control
    // to `slots[0]`, so paragraphs B and C could not be answered at all.
    const payload = {
      slots: [
        { key: "s1", paragraph: "A" },
        { key: "s2", paragraph: "B" },
        { key: "s3", paragraph: "C" },
      ],
    };
    const out = paragraphSlots(payload, SLOT_KEYS);
    expect(out).toEqual([
      { key: "s1", paragraph: "A" },
      { key: "s2", paragraph: "B" },
      { key: "s3", paragraph: "C" },
    ]);
    expect(new Set(out.map((s) => s.key)).size).toBe(3);
  });

  it("falls back to the question's slot keys when `slots` is absent", () => {
    expect(paragraphSlots({}, SLOT_KEYS)).toEqual([
      { key: "s1", paragraph: "s1" },
      { key: "s2", paragraph: "s2" },
      { key: "s3", paragraph: "s3" },
    ]);
  });

  it("falls back when `slots` is not a list", () => {
    expect(paragraphSlots({ slots: "A,B,C" }, SLOT_KEYS)).toHaveLength(3);
    expect(paragraphSlots({ slots: { s1: "A" } }, SLOT_KEYS)).toHaveLength(3);
  });

  it("skips entries that are not {key, paragraph} objects", () => {
    // The payload is an open object on the wire; one bad entry must not take
    // the well-formed ones with it.
    const payload = {
      slots: [null, "s1", { key: "s1" }, { key: 2, paragraph: "B" }, { key: "s2", paragraph: "B" }],
    };
    expect(paragraphSlots(payload, SLOT_KEYS)).toEqual([{ key: "s2", paragraph: "B" }]);
  });

  it("falls back when every entry is malformed", () => {
    expect(paragraphSlots({ slots: [null, 1] }, SLOT_KEYS)).toEqual(
      SLOT_KEYS.map((key) => ({ key, paragraph: key })));
  });

  it("is empty for a question with no slots at all", () => {
    expect(paragraphSlots({}, [])).toEqual([]);
  });
});

describe("the group's instruction line", () => {
  it("prefers English", () => {
    expect(rubricOf({ uz: "Sarlavhani tanlang", en: "Choose the correct heading." }))
      .toBe("Choose the correct heading.");
  });

  it("falls back to the first locale that holds a string", () => {
    expect(rubricOf({ uz: "Sarlavhani tanlang", ru: "Выберите заголовок" }))
      .toBe("Sarlavhani tanlang");
  });

  it("is null when there is nothing to show", () => {
    // `{}` is what the snapshot carries for a group authored without a rubric.
    expect(rubricOf(undefined)).toBeNull();
    expect(rubricOf({})).toBeNull();
    expect(rubricOf({ en: "" })).toBeNull();
  });

  it("skips values that are not strings", () => {
    // The map is open on the wire; a nested object must not render as
    // "[object Object]" above the question.
    expect(rubricOf({ en: { text: "nested" } })).toBeNull();
    expect(rubricOf({ en: 3, uz: "Sarlavhani tanlang" })).toBe("Sarlavhani tanlang");
  });
});
