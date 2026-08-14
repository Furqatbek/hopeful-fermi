/**
 * No table may head two of its columns with the same word.
 *
 * The attendance table did: `Set`, `Done`, `Late`, `Not started` are counts of
 * pieces of work, and the trailing column is the SHARE of them finished — and
 * it was also headed `Done`. So one row carried "Done 2" and "Done 50%", and
 * which was which had to be inferred from the values underneath.
 *
 * A source assertion rather than a rendering one: this suite has no DOM by
 * design, and the header labels are literals in the JSX. That is enough to
 * catch the mistake, which is made while typing the header row.
 *
 * Only literal `<th>` text is compared. A header built from an expression is
 * skipped rather than guessed at.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const FEATURES = fileURLToPath(new URL("../src/features", import.meta.url));

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return tsxFiles(path);
    return path.endsWith(".tsx") ? [path] : [];
  });
}

/** The literal header labels of each `<thead>` block in a file. */
function headerRows(source: string): { labels: string[]; index: number }[] {
  const rows: { labels: string[]; index: number }[] = [];
  for (const head of source.matchAll(/<thead>([\s\S]*?)<\/thead>/g)) {
    const labels: string[] = [];
    for (const th of head[1]!.matchAll(/<th\b[^>]*>([^<>{}]*?)<\/th>/g)) {
      const text = th[1]!.replace(/\s+/g, " ").trim();
      if (text) labels.push(text.toLowerCase());
    }
    rows.push({ labels, index: head.index });
  }
  return rows;
}

describe("table headers", () => {
  const files = tsxFiles(FEATURES);

  it("finds the console's tables at all", () => {
    // Guards the guard: if the markup ever moves to a component the regex does
    // not see, this suite would pass by finding nothing.
    const withTables = files.filter((f) => headerRows(readFileSync(f, "utf8")).length > 0);
    expect(withTables.length).toBeGreaterThan(8);
  });

  it("never repeats a column name within one table", () => {
    const offences: string[] = [];
    for (const file of files) {
      for (const row of headerRows(readFileSync(file, "utf8"))) {
        const seen = new Set<string>();
        for (const label of row.labels) {
          if (seen.has(label)) {
            offences.push(`${file.slice(FEATURES.length + 1)}: "${label}" twice`);
          }
          seen.add(label);
        }
      }
    }
    expect(offences).toEqual([]);
  });
});
