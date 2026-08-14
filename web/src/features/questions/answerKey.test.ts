/**
 * The answer key a teacher builds without typing JSON.
 *
 * Each case is stated as the thing a person does — "types two spellings", "picks
 * NOT GIVEN" — and asserts the exact object the server will receive, because
 * that object is the contract and `key_schema` is what judges it.
 */

import { describe, expect, it } from "vitest";

import {
  controlFor, emptyValue, fromKey, joinAlternatives, slotCountOf, slotIds,
  splitAlternatives, toKey,
} from "./answerKey";

/** The real shapes, copied from `registry/question_types/*.json`. */
const SENTENCE = {
  authoring: { key_widget: "alternatives_editor" },
  scoring: { primitive: "text_per_slot" },
};
const NOTES = {
  authoring: { key_widget: "slot_alternatives_grid" },
  scoring: { primitive: "text_per_slot" },
};
const TFNG = {
  authoring: { key_widget: "tfng_picker" },
  scoring: {
    primitive: "choice_per_slot",
    options: {
      option_source: "fixed",
      fixed_options: [{ id: "TRUE", text: "TRUE" }, { id: "FALSE", text: "FALSE" },
                      { id: "NOT GIVEN", text: "NOT GIVEN" }],
    },
  },
};
const HEADINGS = {
  authoring: { key_widget: "paragraph_to_heading_grid" },
  scoring: { primitive: "choice_per_slot", options: { option_source: "group.option_bank" } },
};
const MCQ_MULTI = {
  authoring: { key_widget: "multi_option_picker" },
  scoring: { primitive: "set_selection" },
};

describe("which control a type gets", () => {
  it("reads the widget the registry declares, not the type key", () => {
    // The registry's whole bet is that a type can be added to a running system
    // with no redeploy. A switch on `type_key` here would quietly revoke it.
    expect(controlFor(SENTENCE, ["s1"]).kind).toBe("alternatives");
    expect(controlFor(NOTES, ["s1", "s2"]).kind).toBe("alternatives");
    expect(controlFor(TFNG, ["s1"]).kind).toBe("fixed");
    expect(controlFor(HEADINGS, ["s1"]).kind).toBe("pick");
    expect(controlFor(MCQ_MULTI, ["s1"]).kind).toBe("multi");
  });

  it("offers the fixed answers the SCORER matches on", () => {
    // From `fixed_options`, not a constant in the console. A picker offering an
    // answer the marker refuses is worse than no picker.
    const control = controlFor(TFNG, ["s1"]);
    expect(control.kind === "fixed" && control.options)
      .toEqual(["TRUE", "FALSE", "NOT GIVEN"]);
  });

  it("gives a one-answer type exactly one row", () => {
    // `true_false_notgiven`'s key schema requires `s1` and forbids anything
    // else, so a second row could only produce a key the server rejects.
    const control = controlFor(TFNG, ["s1", "s2", "s3"]);
    expect(control.kind === "fixed" && control.slots).toEqual(["s1"]);
  });

  it("falls back to raw JSON for a widget nobody has written", () => {
    // A type registered tomorrow stays authorable. The screen says which widget
    // is missing rather than pretending the key is not needed.
    const control = controlFor({ authoring: { key_widget: "hotspot_grid" } }, ["s1"]);
    expect(control).toEqual({ kind: "raw", widget: "hotspot_grid" });
    expect(controlFor(null, ["s1"]).kind).toBe("raw");
  });
});

describe("what the teacher types", () => {
  it("accepts commas or pipes between spellings", () => {
    // Pipes are what the CSV template uses, so an author who has seen one will
    // type them; commas are what everyone else reaches for. Neither is wrong.
    expect(splitAlternatives("fourteen | 14")).toEqual(["fourteen", "14"]);
    expect(splitAlternatives("fourteen, 14")).toEqual(["fourteen", "14"]);
    expect(splitAlternatives(" metres ,, meters | ")).toEqual(["metres", "meters"]);
  });

  it("drops blanks rather than accepting an empty answer", () => {
    // An empty string in `accept` marks a student who wrote nothing correct.
    expect(splitAlternatives(" , | ")).toEqual([]);
  });
});

describe("the key that reaches the server", () => {
  it("builds the documented shape from two spellings", () => {
    const control = controlFor(SENTENCE, ["s1"]);
    const value = { ...emptyValue(), slots: { s1: "fourteen | 14" } };
    expect(toKey(control, value)).toEqual({
      slots: { s1: { accept: ["fourteen", "14"] } },
    });
  });

  it("only carries case_sensitive when it was asked for", () => {
    // `false` is the schema's default. Writing it explicitly on every key is
    // noise in a column a person reads during a dispute.
    const control = controlFor(SENTENCE, ["s1"]);
    expect(toKey(control, { ...emptyValue(), slots: { s1: "Paris" } }))
      .toEqual({ slots: { s1: { accept: ["Paris"] } } });
    expect(toKey(control, { ...emptyValue(), slots: { s1: "Paris" }, caseSensitive: true }))
      .toEqual({ slots: { s1: { accept: ["Paris"], case_sensitive: true } } });
  });

  it("does not split a CHOSEN option on its comma", () => {
    // A heading is prose and may contain a comma. Splitting it would produce
    // two options, neither of which is in the bank.
    const control = controlFor(HEADINGS, ["s1"]);
    const value = { ...emptyValue(), slots: { s1: "A, the first bridge" } };
    expect(toKey(control, value))
      .toEqual({ slots: { s1: { accept: ["A, the first bridge"] } } });
  });

  it("uses the flat shape for the one type that is not per-slot", () => {
    // mcq_multi scores `set_selection` over the whole question, and its key is
    // `{correct: [...]}` with no slots at all.
    const control = controlFor(MCQ_MULTI, ["s1"]);
    expect(toKey(control, { ...emptyValue(), correct: ["C", "A"] }))
      .toEqual({ correct: ["A", "C"] });
  });

  it("skips a blank row instead of writing an empty slot", () => {
    const control = controlFor(NOTES, ["s1", "s2", "s3"]);
    const value = { ...emptyValue(), slots: { s1: "glass", s2: "  ", s3: "sand" } };
    expect(toKey(control, value)).toEqual({
      slots: { s1: { accept: ["glass"] }, s3: { accept: ["sand"] } },
    });
  });

  it("is null when nothing has been filled in", () => {
    // `POST /questions` treats a missing key as "no key yet", which a draft is
    // allowed to be. `{"slots": {}}` fails `minProperties` and reads to the
    // author as a bug in the form.
    expect(toKey(controlFor(SENTENCE, ["s1"]), emptyValue())).toBeNull();
    expect(toKey(controlFor(MCQ_MULTI, ["s1"]), emptyValue())).toBeNull();
  });
});

describe("opening a key that already exists", () => {
  it("reads a JSON key back into the boxes", () => {
    // So the widget takes over from a key written before it existed, rather
    // than discarding it the moment somebody opens the question.
    const value = fromKey({ slots: { s1: { accept: ["fourteen", "14"] } } });
    expect(value.slots.s1).toBe("fourteen | 14");
  });

  it("carries case sensitivity back", () => {
    expect(fromKey({ slots: { s1: { accept: ["Paris"], case_sensitive: true } } })
      .caseSensitive).toBe(true);
  });

  it("reads the flat mcq_multi shape", () => {
    expect(fromKey({ correct: ["A", "C"] }).correct).toEqual(["A", "C"]);
  });

  it("survives junk rather than throwing at the author", () => {
    for (const junk of [null, undefined, 7, "text", {}, { slots: 3 }, { slots: { s1: 9 } }]) {
      expect(() => fromKey(junk)).not.toThrow();
    }
    expect(fromKey({ slots: { s1: 9 } }).slots).toEqual({});
  });

  it("opens the grid at the size the key describes", () => {
    expect(slotCountOf({ slots: { s1: {}, s2: {}, s3: {} } })).toBe(3);
    expect(slotCountOf(null)).toBe(1);
    expect(slotCountOf({ correct: ["A"] })).toBe(1);
  });

  it("round-trips", () => {
    const key = { slots: { s1: { accept: ["metres", "meters"] }, s2: { accept: ["1805"] } } };
    const control = controlFor(NOTES, slotIds(slotCountOf(key)));
    expect(toKey(control, fromKey(key))).toEqual(key);
  });
});

describe("slot ids", () => {
  it("are the ones both schemas use", () => {
    expect(slotIds(3)).toEqual(["s1", "s2", "s3"]);
  });

  it("never produce an empty grid", () => {
    expect(slotIds(0)).toEqual(["s1"]);
    expect(slotIds(-4)).toEqual(["s1"]);
  });
});

describe("joinAlternatives", () => {
  it("uses the separator the CSV template teaches", () => {
    expect(joinAlternatives(["fourteen", "14"])).toBe("fourteen | 14");
  });
});
