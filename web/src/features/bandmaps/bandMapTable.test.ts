import { describe, expect, it } from "vitest";

import {
  formatRows,
  isHalfBand,
  mappingRows,
  overlapping,
  parseRows,
  tableProblems,
  uncovered,
  type BandRow,
} from "./bandMapTable";

/** The platform READING curve, verbatim from migration 0026. A real, complete
 *  band map: whatever this file rejects, it must not reject this. */
const READING: BandRow[] = [
  { raw_min: 0, raw_max: 3, band: 2 }, { raw_min: 4, raw_max: 5, band: 2.5 },
  { raw_min: 6, raw_max: 7, band: 3 }, { raw_min: 8, raw_max: 9, band: 3.5 },
  { raw_min: 10, raw_max: 12, band: 4 }, { raw_min: 13, raw_max: 14, band: 4.5 },
  { raw_min: 15, raw_max: 18, band: 5 }, { raw_min: 19, raw_max: 22, band: 5.5 },
  { raw_min: 23, raw_max: 26, band: 6 }, { raw_min: 27, raw_max: 29, band: 6.5 },
  { raw_min: 30, raw_max: 32, band: 7 }, { raw_min: 33, raw_max: 34, band: 7.5 },
  { raw_min: 35, raw_max: 36, band: 8 }, { raw_min: 37, raw_max: 38, band: 8.5 },
  { raw_min: 39, raw_max: 40, band: 9 },
];

describe("parseRows", () => {
  it("reads a range and a single mark", () => {
    expect(parseRows("30-32 7.0\n40 9").rows).toEqual([
      { raw_min: 30, raw_max: 32, band: 7 },
      { raw_min: 40, raw_max: 40, band: 9 },
    ]);
  });

  it("ignores blank lines and comments", () => {
    expect(parseRows("\n# the 2024 paper\n0-3 2.0\n").rows).toHaveLength(1);
  });

  it("names the line it cannot read rather than dropping it", () => {
    const { rows, errors } = parseRows("0-3 2.0\nthirty to thirty-two: seven");
    expect(rows).toHaveLength(1);
    expect(errors[0]).toContain("Line 2");
  });

  it("refuses a range that runs backwards", () => {
    expect(parseRows("32-30 7.0").errors).toHaveLength(1);
  });

  it("round-trips through formatRows", () => {
    expect(parseRows(formatRows(READING)).rows).toEqual(READING);
  });
});

describe("mappingRows", () => {
  it("sends exactly the three keys the scorer reads", () => {
    // `exam.session` builds the table with `int(row["raw_min"])`, so any other
    // spelling is stored with 201 and raises inside scoring.
    expect(mappingRows([{ raw_min: 0, raw_max: 3, band: 2 }])).toEqual([
      { raw_min: 0, raw_max: 3, band: 2 },
    ]);
    expect(Object.keys(mappingRows(READING)[0] ?? {}).sort())
      .toEqual(["band", "raw_max", "raw_min"]);
  });
});

describe("isHalfBand", () => {
  it("accepts the scale IELTS reports", () => {
    expect([0, 4, 6.5, 9].every(isHalfBand)).toBe(true);
  });

  it("rejects a quarter band and anything off the scale", () => {
    expect(isHalfBand(6.25)).toBe(false);
    expect(isHalfBand(9.5)).toBe(false);
    expect(isHalfBand(-1)).toBe(false);
  });
});

describe("uncovered", () => {
  it("is empty for a complete table", () => {
    expect(uncovered(READING, 40)).toEqual([]);
  });

  it("names every mark with no band", () => {
    expect(uncovered([{ raw_min: 2, raw_max: 3, band: 5 }], 5)).toEqual([0, 1, 4, 5]);
  });

  it("counts a table that starts above zero", () => {
    // The exact fault `exam.scoring._band` documents: "a table starting at 10".
    expect(uncovered([{ raw_min: 10, raw_max: 40, band: 5 }], 40)).toHaveLength(10);
  });
});

describe("overlapping", () => {
  it("is empty for a contiguous table", () => {
    expect(overlapping(READING)).toEqual([]);
  });

  it("finds the marks two rows both claim", () => {
    expect(overlapping([
      { raw_min: 0, raw_max: 4, band: 4 },
      { raw_min: 3, raw_max: 6, band: 5 },
    ])).toEqual([3, 4]);
  });
});

describe("tableProblems", () => {
  it("passes the platform reading curve unchanged", () => {
    expect(tableProblems(READING, 40)).toEqual([]);
  });

  it("refuses an empty table", () => {
    // Accepted by POST /band-maps with 201, measured against the running API.
    expect(tableProblems([], 40)).toHaveLength(1);
    expect(tableProblems([], 40)[0]).toContain("empty");
  });

  it("refuses a gap", () => {
    const problems = tableProblems([{ raw_min: 10, raw_max: 40, band: 5 }], 40);
    expect(problems.some((p) => p.includes("No band for mark"))).toBe(true);
  });

  it("refuses an overlap, because the scorer silently takes the first row", () => {
    const problems = tableProblems([
      { raw_min: 0, raw_max: 40, band: 9 },
      { raw_min: 0, raw_max: 40, band: 2 },
    ], 40);
    expect(problems.some((p) => p.includes("more than one row"))).toBe(true);
  });

  it("refuses a curve that falls", () => {
    const problems = tableProblems([
      { raw_min: 0, raw_max: 20, band: 7 },
      { raw_min: 21, raw_max: 40, band: 5 },
    ], 40);
    expect(problems.some((p) => p.includes("falls as the mark rises"))).toBe(true);
  });

  it("refuses a band off the IELTS scale", () => {
    const problems = tableProblems([{ raw_min: 0, raw_max: 40, band: 6.25 }], 40);
    expect(problems.some((p) => p.includes("6.25"))).toBe(true);
  });

  it("refuses rows above the paper's maximum", () => {
    const problems = tableProblems([{ raw_min: 0, raw_max: 60, band: 5 }], 40);
    expect(problems.some((p) => p.includes("cannot reach"))).toBe(true);
  });

  it("refuses a maximum mark of zero or less", () => {
    // `max_raw: -5` was accepted with 201 by the running API.
    expect(tableProblems(READING, -5)).toHaveLength(1);
    expect(tableProblems(READING, 0)).toHaveLength(1);
  });

  it("reports a short list of marks rather than forty numbers", () => {
    const problems = tableProblems([{ raw_min: 39, raw_max: 40, band: 9 }], 40);
    expect(problems[0]).toContain("more");
  });
});
