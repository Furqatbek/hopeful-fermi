/**
 * A raw-to-band table, and everything that can be wrong with one.
 *
 * Pure and separate from the screen because this is the only guard there is.
 * `POST /band-maps` validates the request model's TYPES and nothing else:
 * measured against the running API, an empty `mapping`, a mapping covering only
 * 10-20 of a 40-mark paper, rows keyed `{lo, hi, b}`, two rows claiming the same
 * raws with different bands, and `max_raw: -5` were each accepted with 201.
 *
 * What that costs is paid later and by somebody else. `exam.session` builds the
 * scorer's table with `int(row["raw_min"])`, so a row with the wrong keys is a
 * KeyError inside scoring — the student's submission fails rather than their
 * band being absent. A raw score covered by no row scores with `band: null`, a
 * result with no grade on it. And the publish gate's `_check_band_map` refuses
 * the test version — `BAND_MAP_GAP`, `BAND_MAP_TOO_SHORT` — which is the same
 * rule as the coverage check below, applied a week later to somebody who did not
 * author the map.
 *
 * So the rules here are the server's own rules, checked at the point where the
 * person who can fix them is looking at them.
 */

export interface BandRow {
  raw_min: number;
  raw_max: number;
  band: number;
}

/** IELTS reports whole and half bands only, 0 to 9. */
export function isHalfBand(band: number): boolean {
  return Number.isFinite(band) && band >= 0 && band <= 9 && band * 2 === Math.round(band * 2);
}

/**
 * One row per line: `raw_min-raw_max band`, or `raw band` for a single mark.
 *
 * A text table rather than a grid of inputs, because a fifteen-row curve is
 * something a centre already has written down. It can be pasted whole, and it
 * can be read back and compared against the paper it came from — which is the
 * check that actually catches a transcription slip.
 */
export function parseRows(text: string): { rows: BandRow[]; errors: string[] } {
  const rows: BandRow[] = [];
  const errors: string[] = [];
  text.split("\n").forEach((raw, index) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    const at = `Line ${index + 1}`;
    const match = /^(\d+)\s*(?:[-–—]\s*(\d+))?[\s,:]+(\d+(?:\.\d+)?)$/.exec(line);
    if (!match) {
      errors.push(`${at}: cannot read "${line}". Write it as "30-32 7.0".`);
      return;
    }
    // Destructured with a length check rather than `!`: under
    // `noUncheckedIndexedAccess` every group is `string | undefined`, and the
    // regex above guarantees groups 1 and 3 only.
    const [, low, high, band] = match;
    if (low === undefined || band === undefined) return;
    const rawMin = Number(low);
    const rawMax = high === undefined ? rawMin : Number(high);
    if (rawMax < rawMin) {
      errors.push(`${at}: ${rawMin}-${rawMax} runs backwards.`);
      return;
    }
    rows.push({ raw_min: rawMin, raw_max: rawMax, band: Number(band) });
  });
  return { rows, errors };
}

/**
 * The rows in the shape the request body declares — `mapping` is an array of
 * free-form objects in the contract, so a typed row is not assignable to it.
 *
 * Written out field by field rather than spread, so the three keys the scorer
 * reads (`raw_min`, `raw_max`, `band`) are the three keys that get sent. A row
 * with any other spelling is stored happily and fails inside scoring.
 */
export function mappingRows(rows: readonly BandRow[]): Record<string, unknown>[] {
  return rows.map((row) => ({
    raw_min: row.raw_min, raw_max: row.raw_max, band: row.band,
  }));
}

/** The same table as text, for prefilling an edit from a map that already exists. */
export function formatRows(rows: readonly BandRow[]): string {
  return rows
    .map((r) => `${r.raw_min}${r.raw_max === r.raw_min ? "" : `-${r.raw_max}`} ${r.band.toFixed(1)}`)
    .join("\n");
}

/** Marks between 0 and `maxRaw` that no row awards a band for. */
export function uncovered(rows: readonly BandRow[], maxRaw: number): number[] {
  const covered = new Set<number>();
  for (const row of rows) {
    for (let mark = Math.max(0, row.raw_min); mark <= Math.min(maxRaw, row.raw_max); mark += 1) {
      covered.add(mark);
    }
  }
  const missing: number[] = [];
  for (let mark = 0; mark <= maxRaw; mark += 1) if (!covered.has(mark)) missing.push(mark);
  return missing;
}

/** Marks awarded a band by more than one row. */
export function overlapping(rows: readonly BandRow[]): number[] {
  const seen = new Set<number>();
  const twice = new Set<number>();
  for (const row of rows) {
    for (let mark = row.raw_min; mark <= row.raw_max; mark += 1) {
      if (seen.has(mark)) twice.add(mark);
      seen.add(mark);
    }
  }
  return [...twice].sort((a, b) => a - b);
}

function list(marks: readonly number[]): string {
  const shown = marks.slice(0, 8).join(", ");
  return marks.length > 8 ? `${shown} and ${marks.length - 8} more` : shown;
}

/**
 * Everything wrong with this table, in the order it matters.
 *
 * Every one of these is accepted by the server today, so an empty result here is
 * the only thing standing between a centre and a cohort marked against a broken
 * curve.
 */
export function tableProblems(
  rows: readonly BandRow[],
  maxRaw: number,
): string[] {
  const problems: string[] = [];
  if (!Number.isInteger(maxRaw) || maxRaw < 1) {
    problems.push("The paper's maximum mark must be a whole number of 1 or more.");
    return problems;
  }
  if (rows.length === 0) {
    problems.push(
      "The table is empty. Every mark would score with no band at all, which is " +
      "a result a student cannot read.",
    );
    return problems;
  }

  const offScale = rows.filter((r) => !isHalfBand(r.band));
  if (offScale.length > 0) {
    problems.push(
      `Not a band on the IELTS scale: ${[...new Set(offScale.map((r) => r.band))].join(", ")}. ` +
      "Bands run 0 to 9 in halves.",
    );
  }

  const over = rows.filter((r) => r.raw_max > maxRaw);
  if (over.length > 0) {
    problems.push(
      `${over.length} row(s) award marks above ${maxRaw}, which this paper cannot reach.`,
    );
  }

  const missing = uncovered(rows, maxRaw);
  if (missing.length > 0) {
    problems.push(
      `No band for mark(s): ${list(missing)}. A student who scores one of those ` +
      "gets a raw count and no band, and the publish gate refuses any test using " +
      "this map.",
    );
  }

  const twice = overlapping(rows);
  if (twice.length > 0) {
    // The scorer returns the FIRST matching row, so an overlap is not a
    // rejection anywhere — it is a silent choice between two answers, decided
    // by the order the rows happen to be stored in.
    problems.push(
      `Mark(s) ${list(twice)} appear in more than one row. Scoring takes whichever ` +
      "row comes first, so the band depends on the order and not on the table.",
    );
  }

  const ordered = [...rows].sort((a, b) => a.raw_min - b.raw_min);
  const backwards = ordered.filter((row, index) => {
    const before = index === 0 ? undefined : ordered[index - 1];
    return before !== undefined && row.band < before.band;
  });
  if (backwards.length > 0) {
    problems.push(
      "The band falls as the mark rises. A student who answers more questions " +
      "correctly would be told they scored lower.",
    );
  }

  return problems;
}
