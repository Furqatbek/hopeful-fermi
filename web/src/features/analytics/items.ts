/**
 * Reading an item analysis: which items are broken, and in what order to look.
 *
 * Pure and separate from the screen because every judgment below is a judgment,
 * not a rendering detail, and each one is wrong in a way a chart would hide.
 *
 * **The server does not tell us enough on its own.** `flag_reasons` is empty
 * below twenty responses — `MIN_RESPONSES` in `app/modules/analytics/stats.py`,
 * because "flagging on noise trains authors to ignore the flags" — and a centre
 * running a class of fifteen is under that line on every item of every mock they
 * ever sit. Trusting `flagged` alone would show that centre a paper with nothing
 * wrong on it, for ever. So the same four rules are applied here to the numbers
 * that ARE returned, and `thin` carries the caveat instead of suppressing the
 * finding. The thresholds below are copied from that file and must not drift
 * from it; where the server did flag, its reasons are kept as well as ours, so
 * this never contradicts the row it is rendering.
 *
 * **`share` has to be computed here.** `stats.analyse` puts one on every
 * `common_wrong` entry and the item-analysis DTO drops it, so the field that
 * decides whether ten students writing the same thing is a missing key
 * alternative or ten careless students does not arrive. Ten of twenty-five is a
 * key problem; ten of four hundred is not, and the count alone cannot tell them
 * apart.
 */

import type { components } from "../../api/schema";

export type Item = components["schemas"]["ItemStat"];
export type FlaggedItem = components["schemas"]["FlaggedItem"];
export type TestVersion = components["schemas"]["TestVersion"];

/** Taken from the contract rather than restated, so a new reason cannot appear
 *  server-side and be silently unhandled here. */
export type Reason = NonNullable<NonNullable<Item["flag_reasons"]>[number]>;

// ── thresholds, mirrored from app/modules/analytics/stats.py ──────────

/** Below this the server refuses to flag at all. Kept as a confidence caveat
 *  here rather than as a gate — see the file comment. */
export const MIN_RESPONSES = 20;
export const NEAR_ZERO_P = 0.05;
export const NEAR_ONE_P = 0.98;
export const COMMON_WRONG_SHARE = 0.1;

/**
 * The share of the WRONG answers that has to be one identical string before it
 * reads as a missing key alternative.
 *
 * Not in the server's file: it has no need of it, since it only decides whether
 * to raise a flag. This screen has to answer the sharper question the author
 * actually asks — "is this string an answer I should be accepting?" — and the
 * count alone does not separate the two cases. Half of everybody who got the
 * item wrong writing the same word is not half a class being careless in the
 * same direction.
 */
export const CONCENTRATED_WRONG = 0.5;

/** What to do about the item, in the words a teacher would use. */
export type Verdict = "broken" | "check-key" | "fine" | "no-data";

export interface Assessment {
  verdict: Verdict;
  /** Most strongly implicating the KEY first, matching `stats.action_for`. */
  reasons: Reason[];
  /** Fewer responses than the server's own evidence threshold, so `flagged` is
   *  false on this row however bad the numbers look. */
  thin: boolean;
  n: number;
}

export interface WrongAnswer {
  value: string;
  count: number;
  /** Of everybody who answered the item. */
  share: number;
  /** Of everybody who got it WRONG. Null when the p-value is missing and the
   *  wrong count therefore cannot be derived. */
  shareOfWrong: number | null;
  /** The one-glance decision this screen exists to make. */
  likelyMissingAnswer: boolean;
}

/**
 * The wrong strings students typed, with the share the API does not send.
 *
 * `likelyMissingAnswer` is deliberately two-signalled. A string given by a tenth
 * of the cohort is only interesting if the OTHER wrong answers are scattered —
 * one popular wrong answer among twenty different ones is a plausible distractor
 * doing its job. The exception is a negative point-biserial: when the students
 * writing it are the strong ones, concentration stops mattering, because that is
 * the signature of an answer that is right and not accepted.
 */
export function wrongAnswers(item: Item): WrongAnswer[] {
  const n = item.n_responses ?? 0;
  if (n <= 0) return [];
  const p = numberOrNull(item.p_value);
  const discrimination = numberOrNull(item.discrimination);
  // Derived, because `n_correct` is on the `item_stats` table and not on either
  // DTO. Rounded: the p-value arrives at four decimal places, so `n * (1 - p)`
  // lands a hair off a whole number of students.
  const wrong = p === null ? null : Math.round(n * (1 - p));

  return (item.common_wrong ?? []).flatMap((entry) => {
    const value = entry.value ?? "";
    const count = entry.count ?? 0;
    // A blank string is what an unanswered item looks like once it is
    // normalised, and it is not an answer anybody chose to give.
    if (!value || count <= 0) return [];
    const share = count / n;
    // Capped: the rounding above can leave a count fractionally above the
    // derived total, and "112% of the wrong answers" reads as a broken screen
    // rather than a broken item.
    const shareOfWrong = wrong !== null && wrong > 0
      ? Math.min(1, count / wrong)
      : null;
    return [{
      value,
      count,
      share,
      shareOfWrong,
      likelyMissingAnswer:
        share >= COMMON_WRONG_SHARE
        && ((shareOfWrong !== null && shareOfWrong >= CONCENTRATED_WRONG)
            || (discrimination !== null && discrimination < 0)),
    }];
  });
}

/**
 * Is this item probably fine, does its key need checking, or is it broken.
 *
 * A negative point-biserial and a near-zero p-value both mean the item measured
 * nothing, and the marks it produced cannot be trusted — that is "broken", and
 * the negative correlation is the stronger of the two because it names the key
 * rather than the question. A popular wrong answer on an item that otherwise
 * discriminates normally is a narrower fault: the item works, one accepted
 * answer is probably missing.
 *
 * `near_one_p` is a reason without being a fault. An item everybody answers
 * correctly is wasting a slot, but nothing about it is wrong and calling it
 * broken next to an item with a bad key would flatten the distinction this
 * screen is for.
 */
export function assess(item: Item): Assessment {
  const n = item.n_responses ?? 0;
  if (n <= 0) return { verdict: "no-data", reasons: [], thin: true, n: 0 };

  const p = numberOrNull(item.p_value);
  const discrimination = numberOrNull(item.discrimination);
  const served = new Set<Reason>(item.flag_reasons ?? []);
  const reasons: Reason[] = [];
  const add = (reason: Reason, holds: boolean) => {
    // Union with what the server flagged: on `/content/flagged-items` the row
    // comes from a projection computed over a ninety-day window that may hold
    // responses this row's own numbers no longer describe, and dropping a reason
    // the server stands behind would make the badge disagree with the action
    // printed beside it.
    if (holds || served.has(reason)) reasons.push(reason);
  };

  add("negative_discrimination", discrimination !== null && discrimination < 0);
  add("common_wrong_answer",
      wrongAnswers(item).some((w) => w.share >= COMMON_WRONG_SHARE));
  add("near_zero_p", p !== null && p < NEAR_ZERO_P);
  add("near_one_p", p !== null && p > NEAR_ONE_P);

  const verdict: Verdict =
    reasons.includes("negative_discrimination") || reasons.includes("near_zero_p")
      ? "broken"
      : reasons.includes("common_wrong_answer")
        ? "check-key"
        : "fine";
  return { verdict, reasons, thin: n < MIN_RESPONSES, n };
}

const RANK: Record<Verdict, number> = {
  broken: 0, "check-key": 1, fine: 2, "no-data": 3,
};

/**
 * Worst first, and within that the most negative discrimination first.
 *
 * The paper's own numbering is the wrong order for this screen and the server's
 * is no better: `/content/flagged-items` sorts by ascending p-value, which puts
 * "nobody could answer it" above "the strong students got it wrong" — the second
 * being the one that is almost always a bad key. Either way the finding sits
 * wherever the item happens to fall on the paper, and a teacher scanning forty
 * rows has to notice a minus sign to find it.
 *
 * Thin evidence sorts after solid evidence WITHIN a verdict rather than below
 * everything. A class of fifteen still gets its broken item at the top, because
 * nothing better-evidenced is competing for the position; a paper with both
 * shows the twenty-five-response finding first.
 *
 * Copies rather than sorting in place: this is handed the array inside a
 * TanStack Query cache entry, and sorting that mutates state React was told is
 * immutable.
 */
export function byConcern<T extends Item>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => {
    const left = assess(a);
    const right = assess(b);
    const rankLeft = RANK[left.verdict] * 2 + (left.thin ? 1 : 0);
    const rankRight = RANK[right.verdict] * 2 + (right.thin ? 1 : 0);
    if (rankLeft !== rankRight) return rankLeft - rankRight;

    if (left.verdict === "broken") {
      const dl = numberOrNull(a.discrimination);
      const dr = numberOrNull(b.discrimination);
      // A null correlation is "cannot be computed", not zero, so it has nothing
      // to rank by and goes to the end of its band instead of into the middle.
      if (dl !== dr) {
        if (dl === null) return 1;
        if (dr === null) return -1;
        return dl - dr;
      }
    }
    if (left.verdict === "check-key") {
      const sl = topWrongShare(a);
      const sr = topWrongShare(b);
      if (sl !== sr) return sr - sl;
    }
    // Always the last comparison, so two identical-looking items keep a fixed
    // order between renders instead of swapping under the reader's eye.
    return (a.number ?? 0) - (b.number ?? 0);
  });
}

/** The share of the cohort behind the single most popular wrong answer. */
export function topWrongShare(item: Item): number {
  return wrongAnswers(item).reduce((most, w) => Math.max(most, w.share), 0);
}

/**
 * One row per question, keeping the one computed over the most responses.
 *
 * `/content/flagged-items` returns a question ONCE PER STATISTICS SCOPE. The
 * projection writes two rows for every sitting that had an organization behind
 * it — the centre's own numbers and the platform-wide total — and the endpoint
 * filters on neither, so a question sat at one centre arrives twice, byte for
 * byte identical, with no field on either row saying which is which. Verified
 * against the real projection sweep: one bad key, twenty-four sittings, two
 * indistinguishable rows.
 *
 * Listing the same broken question twice is a defect a teacher sees, so the
 * broader row wins. The platform row can only ever aggregate MORE responses
 * than the centre row, and "is this question broken" is better answered by more
 * evidence. The centre-only view of the same numbers is the Item analysis
 * screen, which scopes deliberately and says so.
 *
 * Rows with no question to group by are left alone rather than collapsed into
 * one another.
 */
export function dedupeByQuestion<T extends Item>(rows: readonly T[]): T[] {
  const best = new Map<string, T>();
  const ungrouped: T[] = [];
  for (const row of rows) {
    const key = row.question_xid;
    if (!key) {
      ungrouped.push(row);
      continue;
    }
    const held = best.get(key);
    // Strictly greater, so an exact duplicate keeps the row that arrived first
    // and the output does not depend on the server's ordering.
    if (!held || (row.n_responses ?? 0) > (held.n_responses ?? 0)) best.set(key, row);
  }
  return [...best.values(), ...ungrouped];
}

export interface Tally {
  broken: number;
  checkKey: number;
  fine: number;
  noData: number;
}

/** How the paper looks in one line, before any row is read. */
export function summarise(items: readonly Item[]): Tally {
  const tally: Tally = { broken: 0, checkKey: 0, fine: 0, noData: 0 };
  for (const item of items) {
    const { verdict } = assess(item);
    if (verdict === "broken") tally.broken += 1;
    else if (verdict === "check-key") tally.checkKey += 1;
    else if (verdict === "fine") tally.fine += 1;
    else tally.noData += 1;
  }
  return tally;
}

export interface Bar {
  side: "negative" | "positive";
  /** Of the half-track on that side. */
  percent: number;
}

/**
 * Discrimination as a signed bar: away from a centre line, left for negative.
 *
 * A point-biserial is a correlation and lives in −1..1, so it gets an encoding
 * with a sign in it. A plain length would make −0.6 and +0.6 the same picture
 * and leave the minus sign — the entire finding — as something the reader has to
 * spot in a column of numbers.
 */
export function discriminationBar(value: number | null | undefined): Bar | null {
  const correlation = numberOrNull(value);
  if (correlation === null) return null;
  // Clamped, not trusted: the value is rounded server-side and a stored
  // projection row is whatever was written into the column.
  const clamped = Math.max(-1, Math.min(1, correlation));
  return {
    side: clamped < 0 ? "negative" : "positive",
    percent: Math.round(Math.abs(clamped) * 100),
  };
}

/** Mean time as a teacher would say it, or null when there is nothing to say. */
export function meanTime(ms: number | null | undefined): string | null {
  const value = numberOrNull(ms);
  // Zero means "the client reported no timing", which the server is careful to
  // send as null from the live endpoint. A stored projection row is not as
  // careful, and "0s" would read as an item every student skipped.
  if (value === null || value <= 0) return null;
  const seconds = Math.round(value / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

/** A p-value as a percentage correct, which is what it means. */
export function percentCorrect(p: number | null | undefined): string {
  const value = numberOrNull(p);
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

/**
 * The version to analyse when the author has only picked a test.
 *
 * A draft has never been sat, so opening on one shows an empty screen and
 * suggests the paper has no problems. Published first, then archived — an
 * archived version is not assignable any more but everything sat against it
 * still counts, and a paper is usually archived precisely because something was
 * wrong with it.
 */
export function preferredVersion(
  versions: readonly TestVersion[],
): TestVersion | null {
  const newestFirst = [...versions].sort((a, b) => b.version_no - a.version_no);
  return newestFirst.find((v) => v.status === "published")
    ?? newestFirst.find((v) => v.status === "archived")
    ?? newestFirst[0]
    ?? null;
}

/** `null` for a JSON null, an absent field, or a NaN — all of which mean "no
 *  number", and none of which may become a confident zero. */
function numberOrNull(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
