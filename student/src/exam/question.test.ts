import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { QuestionView, paragraphSlots, rubricOf, type Group, type Question } from "./Question";

// No DOM in this environment, so the renderer is proved through its static
// markup — enough to count controls and find the rubric, which is what the
// helpers below cannot prove about the component that uses them.
function markup(question: Question, group: Group): string {
  return renderToStaticMarkup(
    createElement(QuestionView, { question, group, answers: {}, onAnswer: () => undefined }));
}

const BANK: Group = {
  option_bank: [{ id: "i", text: "Heading i" }, { id: "ii", text: "Heading ii" }],
  questions: [],
};

describe("rendering a matching_headings question", () => {
  const question: Question = {
    number: 5, question_version_xid: "qv1", type_key: "matching_headings",
    slot_keys: ["s1", "s2", "s3"],
    payload: { slots: [{ key: "s1", paragraph: "A" }, { key: "s2", paragraph: "B" }, { key: "s3", paragraph: "C" }] },
  };

  it("draws one control per paragraph, numbered on from the question", () => {
    // The helper returning three slots proves nothing if the component still
    // binds one control to `slots[0]`; this is the wiring.
    const html = markup(question, BANK);
    expect((html.match(/<select/g) ?? []).length).toBe(3);
    expect(html).toContain('aria-label="Question 6, paragraph B"');
    expect(html).toContain('aria-label="Question 7, paragraph C"');
  });

  it("leaves the single-slot matching types on one control", () => {
    const html = markup({ ...question, type_key: "matching_information" }, BANK);
    expect((html.match(/<select/g) ?? []).length).toBe(1);
  });
});

describe("rendering the group's instruction line", () => {
  const question: Question = {
    number: 14, question_version_xid: "qv1", type_key: "matching_headings",
    slot_keys: ["s1"], payload: { slots: [{ key: "s1", paragraph: "A" }] },
  };

  it("prints the rubric above the question", () => {
    const html = markup(question, { ...BANK, instructions: { en: "Choose the correct heading." } });
    expect(html).toContain('<p class="q__instruction">Choose the correct heading.</p>');
  });

  it("prints nothing for a group authored without one", () => {
    expect(markup(question, BANK)).not.toContain("q__instruction");
  });
});

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
