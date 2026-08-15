/**
 * The question-body builders, checked against the shapes the registry declares.
 *
 * Every expectation here is the literal object `payload_schema` will judge, and
 * the schemas set `additionalProperties: false` — so a property named on a
 * guess is rejected outright by the server rather than ignored. Two of these
 * tests exist because exactly that happened while writing the module: a table
 * cell's text is `value`, not `text`, and a form's blank carries `slot` rather
 * than a `value` holding the marker.
 */

import { describe, expect, it } from "vitest";

import {
  blankText, blankTextValue, flowSteps, formFields, formatTimestamp, keyFor,
  nextMarker, noteBlocks, normaliseMarkers, parseTimestamp, slotIdsOf, slotList,
  slotListValues, slotsInLines, slotsInText, tableGrid,
} from "./payloadParts";

describe("blank markers", () => {
  it("finds the slots a line refers to, in reading order", () => {
    expect(slotsInText("It opened in {{s2}} and cost {{s1}}.")).toEqual(["s2", "s1"]);
  });

  it("accepts the {{1}} people actually type", () => {
    expect(slotsInText("cost {{1}}")).toEqual(["s1"]);
    expect(normaliseMarkers("cost {{1}} and {{ s2 }}")).toBe("cost {{s1}} and {{s2}}");
  });

  it("does not sort, so s10 stays after s2", () => {
    // Numeric sorting would reorder a paper's blanks behind the author's back.
    expect(slotsInLines(["{{s2}}", "{{s10}}", "{{s1}}"])).toEqual(["s2", "s10", "s1"]);
  });

  it("counts a repeated marker once", () => {
    expect(slotsInText("{{s1}} then {{s1}} again")).toEqual(["s1"]);
  });

  it("ignores things that only look like markers", () => {
    expect(slotsInText("{s1} {{a}} {{}} {{ s }}")).toEqual([]);
  });
});

describe("a numbered slot list", () => {
  it("assigns the keys so the author never types s1", () => {
    expect(slotList("label", ["the tower", "the pier"]))
      .toEqual([{ key: "s1", label: "the tower" }, { key: "s2", label: "the pier" }]);
  });

  it("renumbers after a deletion instead of leaving a gap", () => {
    // A gap at s2 is legal against the schema and wrong for a paper: blanks are
    // numbered by where they are, not by what survived an edit.
    expect(slotList("paragraph", ["A", "", "C"]))
      .toEqual([{ key: "s1", paragraph: "A" }, { key: "s2", paragraph: "C" }]);
  });

  it("drops an empty row rather than sending an unlabelled slot", () => {
    expect(slotList("hint", ["  ", ""])).toEqual([]);
  });

  it("reads an existing list back into the boxes", () => {
    expect(slotListValues("label", { slots: [{ key: "s1", label: "the tower" }] }))
      .toEqual(["the tower"]);
    expect(slotListValues("label", null)).toEqual([""]);
    expect(slotListValues("label", { slots: "nonsense" })).toEqual([""]);
  });
});

describe("notes", () => {
  it("derives the slot list from the markers", () => {
    // The schema requires `slots` ALONGSIDE the text, so a hand-authoring
    // teacher had to keep two things in agreement. One source of truth now.
    expect(noteBlocks([
      { kind: "heading", text: "The bridge" },
      { kind: "bullet", text: "Opened in {{s1}}" },
      { kind: "bullet", text: "Cost {{2}}" },
    ])).toEqual({
      blocks: [
        { kind: "heading", text: "The bridge" },
        { kind: "bullet", text: "Opened in {{s1}}" },
        { kind: "bullet", text: "Cost {{s2}}" },
      ],
      slots: ["s1", "s2"],
    });
  });

  it("drops empty blocks", () => {
    expect(noteBlocks([{ kind: "line", text: "  " }]))
      .toEqual({ blocks: [], slots: [] });
  });
});

describe("a flowchart", () => {
  it("carries a branch only when there is one", () => {
    expect(flowSteps([
      { text: "Collect {{s1}}", branch: "" },
      { text: "Heat it", branch: "if cold" },
    ])).toEqual({
      steps: [{ text: "Collect {{s1}}" },
              { text: "Heat it", branch: "if cold" }],
      slots: ["s1"],
    });
  });
});

describe("a form", () => {
  it("sends a blank as `slot`, not as a value holding the marker", () => {
    // The schema names both properties and forbids anything else. A blank sent
    // as `value: "{{s1}}"` renders the marker to the student instead of a box.
    expect(formFields([{ label: "Surname", value: "{{s1}}" }]))
      .toEqual({ fields: [{ label: "Surname", slot: "s1" }], slots: ["s1"] });
  });

  it("sends context as `value`", () => {
    expect(formFields([{ label: "Town", value: "Tashkent" }]))
      .toEqual({ fields: [{ label: "Town", value: "Tashkent" }], slots: [] });
  });

  it("treats a value that merely CONTAINS a marker as text", () => {
    // "Flat {{s1}}" is a label with a gap in it, not a bare blank, and the two
    // render differently.
    expect(formFields([{ label: "Address", value: "Flat {{s1}}" }]))
      .toEqual({ fields: [{ label: "Address", value: "Flat {{s1}}" }], slots: ["s1"] });
  });

  it("keeps a label with no value at all", () => {
    expect(formFields([{ label: "Notes", value: "" }]))
      .toEqual({ fields: [{ label: "Notes" }], slots: [] });
  });
});

describe("a table", () => {
  it("names a text cell `value`, which is what the schema allows", () => {
    // `text` is the obvious guess and `additionalProperties: false` rejects it.
    const grid = tableGrid(["Year", "Cost"], [["1805", "{{s1}}"]]) as {
      rows: { kind: string; value?: string; slot?: string }[][];
    };
    expect(grid.rows[0]![0]).toEqual({ kind: "text", value: "1805" });
  });

  it("makes a cell that is ONLY a marker the blank", () => {
    const grid = tableGrid(["Year", "Cost"], [["1805", "{{s1}}"]]) as {
      rows: { kind: string; slot?: string }[][];
    };
    expect(grid.rows[0]![1]).toEqual({ kind: "blank", slot: "s1" });
  });

  it("builds columns, rows and the derived slots together", () => {
    expect(tableGrid(["Year", "Cost"], [["1805", "{{s1}}"], ["1890", "{{s2}}"]]))
      .toEqual({
        columns: ["Year", "Cost"],
        rows: [
          [{ kind: "text", value: "1805" }, { kind: "blank", slot: "s1" }],
          [{ kind: "text", value: "1890" }, { kind: "blank", slot: "s2" }],
        ],
        slots: ["s1", "s2"],
      });
  });

  it("drops a wholly empty row and trims to the columns declared", () => {
    const grid = tableGrid(["Year"], [["1805", "spare"], ["", ""]]) as {
      rows: unknown[][];
    };
    expect(grid.rows).toHaveLength(1);
    expect(grid.rows[0]).toHaveLength(1);
  });
});

describe("keyFor", () => {
  it("is one-based, like every slot id in the registry", () => {
    expect([0, 1, 9].map(keyFor)).toEqual(["s1", "s2", "s10"]);
  });
});

describe("one passage of prose with blanks in it", () => {
  // These three types — sentence completion and both summary completions —
  // could not be taken from the console to a published paper at all. The body
  // fell back to a JSON box and `slots` had NO field on the form, so the only
  // payload the console could build was `{text}`. `POST /questions` took it,
  // because the payload is checked at publish and not on write, and the gate
  // then refused the whole paper with
  //   PAYLOAD_INVALID  Invalid question content — 'slots' is a required property
  // Driven end to end against a live stack before this was written.

  it("derives the slots the schema requires beside the text", () => {
    expect(blankText("text", "The bridge opened in {{s1}} and cost {{s2}}."))
      .toEqual({
        text: "The bridge opened in {{s1}} and cost {{s2}}.",
        slots: ["s1", "s2"],
      });
  });

  it("writes the field the TYPE names, not a field called text", () => {
    // `summary_completion` carries its prose in `summary`, and the schemas set
    // `additionalProperties: false` — a payload with `text` in it is refused
    // outright rather than ignored.
    expect(Object.keys(blankText("summary", "A summary with {{s1}}.")).sort())
      .toEqual(["slots", "summary"]);
  });

  it("takes the markers people actually type", () => {
    expect(blankText("text", "Opened in {{1}}, closed in {{ s2 }}."))
      .toEqual({ text: "Opened in {{s1}}, closed in {{s2}}.", slots: ["s1", "s2"] });
  });

  it("reports no slots for prose with no blanks, rather than inventing one", () => {
    // `minItems: 1` refuses it at publish, which is the right answer: a
    // completion question with nothing to complete is not a question. Inventing
    // `s1` here would produce a key row for a blank the student never sees.
    expect(blankText("text", "Nothing to fill in here.").slots).toEqual([]);
  });

  it("round-trips through the box", () => {
    const built = blankText("summary", "  Trade grew after {{s1}}.  ");
    expect(blankTextValue("summary", built)).toBe("Trade grew after {{s1}}.");
    expect(blankTextValue("summary", {})).toBe("");
    expect(blankTextValue("summary", null)).toBe("");
  });
});

describe("inserting the next blank", () => {
  it("starts at one", () => {
    expect(nextMarker("")).toBe("{{s1}}");
    expect(nextMarker("No blanks yet.")).toBe("{{s1}}");
  });

  it("counts past the HIGHEST, not past the count", () => {
    // Delete the middle blank of three and insert again: counting the survivors
    // would hand out a second {{s3}}. Two blanks sharing an id is one answer
    // key entry for two boxes, and the student's second answer would be marked
    // against the first one's accepted list.
    expect(nextMarker("Opened in {{s1}} and cost {{s3}}.")).toBe("{{s4}}");
  });
});

describe("an audio cue", () => {
  it("takes the time a teacher reads off the player", () => {
    expect(parseTimestamp("1:32")).toBe(92_000);
    expect(parseTimestamp("0:07")).toBe(7_000);
    expect(parseTimestamp("12:00")).toBe(720_000);
  });

  it("takes a bare number as SECONDS", () => {
    // Somebody typing 90 into a box labelled with a time means a minute and a
    // half. Reading it as milliseconds would put the cue a tenth of a second in
    // and nothing would look wrong.
    expect(parseTimestamp("90")).toBe(90_000);
  });

  it("is undefined for empty and for nonsense, never zero", () => {
    // The field is optional, and 0 is a different claim: it marks the cue at
    // the very start of the track.
    expect(parseTimestamp("")).toBeUndefined();
    expect(parseTimestamp("   ")).toBeUndefined();
    expect(parseTimestamp("about a minute")).toBeUndefined();
    expect(parseTimestamp("1:2:3")).toBeUndefined();
    expect(parseTimestamp("1:")).toBeUndefined();
  });

  it("shows a stored value back as m:ss", () => {
    expect(formatTimestamp(92_000)).toBe("1:32");
    expect(formatTimestamp(7_000)).toBe("0:07");
    expect(formatTimestamp(0)).toBe("0:00");
    expect(formatTimestamp(undefined)).toBe("");
    expect(formatTimestamp(-5)).toBe("");
  });

  it("round-trips", () => {
    for (const written of ["0:00", "1:32", "12:05"]) {
      expect(formatTimestamp(parseTimestamp(written))).toBe(written);
    }
  });
});

describe("the blanks the body has already settled", () => {
  // The answer key needs one row per blank. Counting them again by hand is a
  // decision the author has already made in the sentence above — and one they
  // can restate wrong, which is `KEY_SLOTS_MISSING` on a paper rather than a
  // hint on a form.

  it("reads the array the text builders derive", () => {
    expect(slotIdsOf(blankText("text", "Opened in {{s1}}, cost {{s2}}.")))
      .toEqual(["s1", "s2"]);
  });

  it("reads the keys a slot LIST carries, which is the other shape in the wild", () => {
    expect(slotIdsOf({ slots: slotList("label", ["the ticket office", "the bridge"]) }))
      .toEqual(["s1", "s2"]);
  });

  it("is null when the payload settles nothing, so the manual control stays", () => {
    // `mcq_single` and the true/false types have no `slots` at all, and their
    // key is one choice. Returning [] would silently offer zero rows.
    expect(slotIdsOf({ stem: "Which of these…", options: [] })).toBeNull();
    expect(slotIdsOf({})).toBeNull();
    expect(slotIdsOf(null)).toBeNull();
    expect(slotIdsOf({ slots: [] })).toBeNull();
  });

  it("ignores anything that is not a slot id", () => {
    expect(slotIdsOf({ slots: ["s1", "", "nonsense", { key: "s2" }, { hint: "x" }] }))
      .toEqual(["s1", "s2"]);
  });
});
