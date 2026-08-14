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
  flowSteps, formFields, keyFor, noteBlocks, normaliseMarkers, slotList,
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
