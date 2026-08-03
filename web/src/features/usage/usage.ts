/**
 * Reading a usage report, as pure functions.
 *
 * `UsageReport` carries `published_count` and `draft_count`, and the second name
 * is not what it says. The server computes it as "everything that is not
 * published" — `_usage()` in `assets.py` partitions the rows on
 * `status == "published"` and calls the remainder drafts — so an ARCHIVED test
 * version is counted as a draft. Rendering that number under the word "drafts"
 * would tell an author that a paper still being written references this passage
 * when in fact a retired one does, and the two lead to opposite decisions.
 *
 * So the tally is rebuilt from `references[].status`, which carries the real
 * status per row, and falls back to the server's two numbers only when the list
 * is absent. The counts are then never wrong in a way that changes a decision.
 */

import type { components } from "../../api/schema";

export type UsageReport = components["schemas"]["UsageReport"];
export type UsageReference = NonNullable<UsageReport["references"]>[number];

export interface Tally {
  /** Frozen at publish, so an edit here cannot change them. */
  published: number;
  /** Not yet frozen: they will take this material as it stands when published. */
  draft: number;
  /** Retired. Nothing new can be sat against them. */
  archived: number;
  /** A status this console does not know. Counted, never hidden. */
  other: number;
  total: number;
}

const EMPTY: Tally = { published: 0, draft: 0, archived: 0, other: 0, total: 0 };

export function tally(report: UsageReport | undefined): Tally {
  if (!report) return EMPTY;
  const rows = report.references ?? [];
  if (rows.length === 0) {
    // No list to count. Trust the server's own arithmetic rather than reporting
    // zero, and keep the archived rows it folded into `draft_count` where it put
    // them — inventing a split we cannot see would be worse than a coarse one.
    const published = report.published_count ?? 0;
    const draft = report.draft_count ?? 0;
    return { published, draft, archived: 0, other: 0, total: published + draft };
  }
  const counted = { ...EMPTY, total: rows.length };
  for (const row of rows) {
    if (row.status === "published") counted.published += 1;
    else if (row.status === "archived") counted.archived += 1;
    else if (row.status === "draft") counted.draft += 1;
    else counted.other += 1;
  }
  return counted;
}

/** Where a status sits in the ordering below. Lower sorts first. */
const RISK: Record<string, number> = { published: 0, in_review: 1, draft: 2, archived: 3 };

/**
 * The references an author has to look at first, first.
 *
 * Published before draft before archived. With twenty references the ordering is
 * the difference between seeing the live paper and scrolling past it: a
 * published version is the one somebody may be sitting, and an archived one is
 * the one that no longer matters. Ties keep title order so the list does not
 * reshuffle between two renders of the same data.
 */
export function byRisk(references: readonly UsageReference[] | undefined): UsageReference[] {
  return [...(references ?? [])].sort((a, b) => {
    const rank = (RISK[a.status ?? ""] ?? 9) - (RISK[b.status ?? ""] ?? 9);
    return rank !== 0 ? rank : (a.title ?? "").localeCompare(b.title ?? "");
  });
}
