/**
 * The option bank, which the author should never have to number.
 *
 * The old field took `A = Living near water` one line at a time, so every
 * identifier was typed by hand and every duplicate, gap and stray separator was
 * the teacher's problem. Position decides the identifier; these pin that, and
 * that a list pasted off a real paper still lands correctly.
 */

import { describe, expect, it } from "vitest";

import {
  type Option, bankProblem, formatBank, identifier, parseBank, stripIdentifier,
  styleOf,
} from "./optionBank";

describe("identifiers", () => {
  it("counts in capitals for an ordinary bank", () => {
    expect([0, 1, 2].map((i) => identifier(i, "letters"))).toEqual(["A", "B", "C"]);
  });

  it("counts in lower-case roman for headings, as a real paper does", () => {
    expect([0, 3, 8].map((i) => identifier(i, "roman"))).toEqual(["i", "iv", "ix"]);
  });

  it("does not silently run out of letters", () => {
    // A bank this long is already a mistake; producing `undefined` beside a
    // heading would be a worse one.
    expect(identifier(26, "letters")).toBe("AA");
    expect(identifier(25, "letters")).toBe("Z");
  });
});

describe("what the author types", () => {
  it("is the words only — the letters are assigned", () => {
    expect(parseBank("Living near water\nThe city's first bridge", "letters"))
      .toEqual([
        { id: "A", text: "Living near water" },
        { id: "B", text: "The city's first bridge" },
      ]);
  });

  it("numbers headings in roman", () => {
    expect(parseBank("Trade\nWar", "roman"))
      .toEqual([{ id: "i", text: "Trade" }, { id: "ii", text: "War" }]);
  });

  it("takes a list pasted off a paper that already has its letters", () => {
    // The whole task is pasting eight headings from another window. Leaving the
    // identifiers in would render "A — A. Trade"; refusing the paste is worse.
    const pasted = "A. Trade routes\nB) War and peace\nC = Rivers\niv — Glassmaking";
    expect(parseBank(pasted, "letters").map((o) => o.text))
      .toEqual(["Trade routes", "War and peace", "Rivers", "Glassmaking"]);
  });

  it("renumbers a pasted list from position, not from what was pasted", () => {
    // Pasting C, A, B must not produce a bank with those letters against those
    // rows — the order on screen is the order a student sees.
    expect(parseBank("C = Rivers\nA = Trade\nB = War", "letters"))
      .toEqual([
        { id: "A", text: "Rivers" }, { id: "B", text: "Trade" },
        { id: "C", text: "War" },
      ]);
  });

  it("keeps a heading that is one short word", () => {
    // The first version matched one to three letters with an OPTIONAL
    // separator, so "War", "Sea", "Oil" and "Tax" were each read as a dangling
    // identifier and deleted from the bank in silence. A paper would have gone
    // out with headings missing.
    expect(parseBank("Trade\nWar\nSea\nOil\nTax", "letters").map((o) => o.text))
      .toEqual(["Trade", "War", "Sea", "Oil", "Tax"]);
  });

  it("keeps a short heading that carries a colon", () => {
    // The same pattern turned "Tax: who paid" into "who paid".
    expect(parseBank("Tax: who paid", "letters")[0]!.text).toBe("Tax: who paid");
  });

  it("does not eat the first letter of an ordinary word", () => {
    // A greedy strip turns "Apples and pears" into "pples and pears".
    expect(parseBank("Apples and pears\nIrrigation", "letters").map((o) => o.text))
      .toEqual(["Apples and pears", "Irrigation"]);
  });

  it("keeps a heading that contains its own separator", () => {
    expect(parseBank("Trade — and why it mattered", "letters")[0]!.text)
      .toBe("Trade — and why it mattered");
  });

  it("drops blank lines and a stray identifier on its own", () => {
    expect(parseBank("Trade\n\n  \nB.\nWar", "letters").map((o) => o.text))
      .toEqual(["Trade", "War"]);
  });

  it("survives a line that is nothing but a separator", () => {
    expect(() => parseBank("—\n=\n", "letters")).not.toThrow();
  });
});

describe("opening a bank that already exists", () => {
  it("shows the words back, without the identifiers", () => {
    const bank: Option[] = [{ id: "A", text: "Trade" }, { id: "B", text: "War" }];
    expect(formatBank(bank)).toBe("Trade\nWar");
    expect(formatBank(undefined)).toBe("");
  });

  it("keeps the style it was saved in", () => {
    // Opening a roman bank and saving it must not renumber every heading to
    // capitals behind the author's back.
    expect(styleOf([{ id: "iii", text: "x" }])).toBe("roman");
    expect(styleOf([{ id: "C", text: "x" }])).toBe("letters");
    expect(styleOf([])).toBe("letters");
  });

  it("round-trips", () => {
    const text = "Trade\nWar\nRivers";
    expect(formatBank(parseBank(text, "roman"))).toBe(text);
  });
});

describe("what is wrong with a bank", () => {
  it("says nothing about an empty one", () => {
    // Most types have no bank at all, and an empty field is not an error.
    expect(bankProblem([], 3, 16)).toBeNull();
  });

  it("counts against the bounds the registry declares", () => {
    expect(bankProblem(parseBank("one\ntwo", "letters"), 3, 16))
      .toMatch(/at least 3/);
    expect(bankProblem(parseBank("x\n".repeat(3), "letters"), 0, 2))
      .toMatch(/at most 2/);
  });

  it("catches a repeated option, which has no right answer", () => {
    expect(bankProblem(parseBank("Trade\nWar\ntrade", "letters")))
      .toMatch(/twice/);
  });

  it("passes a good bank", () => {
    expect(bankProblem(parseBank("Trade\nWar\nRivers", "letters"), 2, 16)).toBeNull();
  });
});

describe("stripIdentifier", () => {
  it("leaves a line that carries no identifier alone", () => {
    expect(stripIdentifier("Living near water")).toBe("Living near water");
  });

  it("never returns empty for a line that had content", () => {
    expect(stripIdentifier("A.")).toBe("A.");
  });
});
