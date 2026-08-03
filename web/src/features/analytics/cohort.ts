/**
 * The arithmetic behind the two cohort screens, kept out of them.
 *
 * Three of these exist because of what the endpoints actually return, which is
 * not what the contract declares. All three were observed against the running
 * API, not inferred:
 *
 * **`GET /cohorts/{xid}/progress` returns its numbers as JSON STRINGS.** Every
 * numeric column in it comes from a PostgreSQL `numeric` — `sum()` over a bigint
 * and `round(avg(...), 1)` both produce one — which arrives as a `Decimal` and is
 * serialized as `"5.5"`. The generated types say `number`, so `avg_band.toFixed(1)`
 * compiles and throws at runtime. `numeric()` is the only way a band from that
 * endpoint should ever be read.
 *
 * **`late` is a subset of `completed`, not an alternative to it.** The projection
 * sets `completed` from the attempt's status and `late` from
 * `submitted_at > closes_at`, so a late submission is counted in both. Subtracting
 * is what turns four overlapping counts into four segments that add up.
 *
 * **A per-student band cannot be read as progress.** `first_band` is `min(avg_band)`
 * and `latest_band` is `max(best_band)` across every week, so they are the lowest
 * and highest weeks in some order, never the first and last. A student who went
 * 7.0 then 5.0 is reported as `first 5.0, latest 7.0, delta +2.0`. Named
 * `lowest`/`highest`/`spread` here so the screen cannot present a decline as a
 * gain; the cohort trend that CAN go negative is `trend()`, off the weekly series.
 */

/** Band scale for bar heights. The floor and ceiling of `results/distribution.ts`,
 *  so one band is one height everywhere in the console. */
export const BAND_FLOOR = 4;
export const BAND_CEILING = 9;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

/**
 * A number from whatever the endpoint sent, or null.
 *
 * `Number("")` is 0 and `Number(" ")` is 0, so blank has to be rejected before
 * parsing — otherwise a missing band renders as a confident 0.0, which on a
 * 4-to-9 scale reads as a catastrophic result rather than as no result.
 */
export function numeric(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A whole count, never negative and never blank. */
export function whole(value: unknown): number {
  const parsed = numeric(value);
  if (parsed === null || parsed < 0) return 0;
  return Math.round(parsed);
}

/** One band to one decimal, or an em dash. Never "0.0" for a missing band. */
export function band(value: number | null): string {
  return value === null ? "—" : value.toFixed(1);
}

/**
 * A week's label without constructing a `Date`.
 *
 * `new Date("2026-07-06")` is midnight UTC, and `toLocaleDateString` in any
 * timezone west of it renders the 5th — so a week starting Monday would be
 * labelled Sunday for part of the world. The value is a plain calendar date and
 * is formatted as one.
 */
export function weekLabel(week: string): string {
  const [, month, day] = week.split("-");
  const index = Number(month) - 1;
  const name = MONTHS[index];
  if (name === undefined || day === undefined) return week;
  return `${Number(day)} ${name}`;
}

// ── attendance ───────────────────────────────────────────────────────

export interface AttendanceRowLike {
  user?: { xid?: string | undefined;
           given_name?: string | undefined;
           family_name?: string | null | undefined } | undefined;
  assigned?: unknown;
  started?: unknown;
  completed?: unknown;
  late?: unknown;
}

/** The four counts as the endpoint reports them, coerced but not adjusted. */
export interface AttendanceCounts {
  assigned: number;
  started: number;
  completed: number;
  late: number;
}

/** The same work split into parts that do not overlap and do add up. */
export interface AttendanceSegments {
  onTime: number;
  late: number;
  /** Started and not finished. */
  unfinished: number;
  notStarted: number;
}

export function counts(row: AttendanceRowLike): AttendanceCounts {
  return {
    assigned: whole(row.assigned),
    started: whole(row.started),
    completed: whole(row.completed),
    late: whole(row.late),
  };
}

/**
 * Four disjoint segments summing to `assigned`.
 *
 * Each input is capped by the one above it before subtracting. In the data the
 * refresh job writes the nesting already holds — an attempt row makes `started`
 * true, a submitted status makes `completed` true, and `late` needs a
 * `submitted_at` — but `late` and `completed` are computed from two independent
 * columns, so a combination that breaks the nesting is possible. Capping makes
 * that render as a shorter bar; without it the subtraction goes negative and the
 * bar overflows its row. The screen still prints `counts.late` verbatim, so the
 * capping can shorten a segment but can never hide a late submission.
 */
export function segments(row: AttendanceCounts): AttendanceSegments {
  const started = Math.min(row.started, row.assigned);
  const completed = Math.min(row.completed, started);
  const late = Math.min(row.late, completed);
  return {
    onTime: completed - late,
    late,
    unfinished: started - completed,
    notStarted: row.assigned - started,
  };
}

/** Completed as a percentage, or null when nothing was ever set — which is a
 *  different fact from 0%, and the difference is whose fault it is. */
export function completion(row: AttendanceCounts): number | null {
  if (row.assigned === 0) return null;
  return Math.round((Math.min(row.completed, row.assigned) / row.assigned) * 100);
}

/** Work this student still owes. The ordering key for "needs attention", chosen
 *  because it needs no weights to defend: not started plus started-and-unfinished. */
export function outstanding(parts: AttendanceSegments): number {
  return parts.notStarted + parts.unfinished;
}

export function personName(user: AttendanceRowLike["user"]): string {
  const parts = [user?.given_name, user?.family_name].filter(
    (part): part is string => typeof part === "string" && part.trim() !== "");
  if (parts.length > 0) return parts.join(" ");
  // A name is how a parent finds their child on this page. With none, the xid is
  // at least a handle staff can match against the class list.
  return user?.xid ? `Student ${user.xid.slice(0, 8)}` : "Unnamed student";
}

export interface StudentAttendance {
  key: string;
  name: string;
  counts: AttendanceCounts;
  parts: AttendanceSegments;
  completion: number | null;
}

export function attendanceRows(
  rows: readonly AttendanceRowLike[],
): StudentAttendance[] {
  return rows.map((row, index) => {
    const totals = counts(row);
    return {
      key: row.user?.xid ?? `row-${index}`,
      name: personName(row.user),
      counts: totals,
      parts: segments(totals),
      completion: completion(totals),
    };
  });
}

export type RowOrder = "name" | "attention";

/**
 * By name to look somebody up; by outstanding work to find who to chase.
 *
 * Name is the default because this page is read with a parent sitting next to
 * you and a specific child in mind. Both orders fall back to the name so the
 * list is stable between renders rather than reshuffling on a tie.
 */
export function orderAttendance(
  rows: readonly StudentAttendance[],
  order: RowOrder,
): StudentAttendance[] {
  const byName = (a: StudentAttendance, b: StudentAttendance) =>
    a.name.localeCompare(b.name);
  if (order === "name") return [...rows].sort(byName);
  return [...rows].sort((a, b) =>
    outstanding(b.parts) - outstanding(a.parts)
    || b.counts.late - a.counts.late
    || byName(a, b));
}

export interface MemberLike {
  user?: { xid?: string | undefined;
           given_name?: string | undefined;
           family_name?: string | null | undefined } | undefined;
  status?: string | undefined;
}

/**
 * Active class members with no attendance row at all.
 *
 * The endpoint aggregates `attendance_facts`, and a student who has never been
 * targeted by an assignment has no row there — so they are absent from the grid
 * entirely rather than showing zeroes. On a page a centre puts in front of a
 * parent, a missing child reads as "not in this class", which is the wrong
 * answer to a question nobody asked out loud.
 */
export function unlisted(
  members: readonly MemberLike[],
  rows: readonly AttendanceRowLike[],
): string[] {
  const present = new Set(
    rows.map((row) => row.user?.xid).filter((xid): xid is string => Boolean(xid)));
  return members
    .filter((member) => member.status !== "left")
    .filter((member) => !member.user?.xid || !present.has(member.user.xid))
    .map((member) => personName(member.user))
    .sort((a, b) => a.localeCompare(b));
}

// ── progress ─────────────────────────────────────────────────────────

export interface WeekRowLike {
  week?: unknown;
  attempts?: unknown;
  avg_band?: unknown;
  reading_band?: unknown;
  listening_band?: unknown;
}

export interface WeekPoint {
  week: string;
  attempts: number;
  overall: number | null;
  reading: number | null;
  listening: number | null;
}

/** The measured skills, in the order the charts show them. */
export const SKILLS = ["overall", "reading", "listening"] as const;
export type Skill = (typeof SKILLS)[number];

/** Sorted ascending. The handler orders by week already; sorting here means the
 *  chart's left-to-right is a property of the chart and not of the SQL. */
export function weekSeries(weeks: readonly WeekRowLike[]): WeekPoint[] {
  return weeks
    .map((row) => ({
      week: typeof row.week === "string" ? row.week : "",
      attempts: whole(row.attempts),
      overall: numeric(row.avg_band),
      reading: numeric(row.reading_band),
      listening: numeric(row.listening_band),
    }))
    .filter((point) => point.week !== "")
    .sort((a, b) => a.week.localeCompare(b.week));
}

/**
 * A bar's height as a percentage of the 4-to-9 scale, or null when there is no
 * band that week.
 *
 * Null rather than 0 so the screen can leave a gap. A zero-height bar and a week
 * nobody sat anything look identical, and one of them is a class that stopped
 * turning up.
 */
export function bandHeight(value: number | null): number | null {
  if (value === null) return null;
  const clamped = Math.min(BAND_CEILING, Math.max(BAND_FLOOR, value));
  return ((clamped - BAND_FLOOR) / (BAND_CEILING - BAND_FLOOR)) * 100;
}

export interface Trend {
  from: number;
  to: number;
  delta: number;
}

/**
 * First week to last week for one skill — the question `delta` on each student
 * cannot answer.
 *
 * Null with fewer than two weeks carrying a band: one point is not a direction,
 * and drawing an arrow from it would be an invention.
 */
export function trend(series: readonly WeekPoint[], skill: Skill): Trend | null {
  const values = series
    .map((point) => point[skill])
    .filter((value): value is number => value !== null);
  const from = values[0];
  const to = values[values.length - 1];
  if (from === undefined || to === undefined || values.length < 2) return null;
  return { from, to, delta: Math.round((to - from) * 10) / 10 };
}

export interface StudentRowLike {
  user?: { xid?: string | undefined;
           given_name?: string | undefined;
           family_name?: string | null | undefined } | undefined;
  attempts?: unknown;
  first_band?: unknown;
  latest_band?: unknown;
}

export interface StudentSpread {
  key: string;
  name: string;
  /** `attempts` on the response counts view ROWS, one per week the student sat
   *  anything — not sittings. Three attempts across two weeks reports 2. */
  weeks: number;
  lowest: number | null;
  highest: number | null;
  spread: number | null;
}

/** Highest band first, students with no band last — the order `Results.tsx`
 *  uses, for the same reason: an absent band sorted as zero reads as a failure. */
export function studentSpreads(
  students: readonly StudentRowLike[],
): StudentSpread[] {
  return students
    .map((row, index) => {
      const lowest = numeric(row.first_band);
      const highest = numeric(row.latest_band);
      return {
        key: row.user?.xid ?? `student-${index}`,
        name: personName(row.user),
        weeks: whole(row.attempts),
        lowest,
        highest,
        spread: lowest === null || highest === null
          ? null
          : Math.round((highest - lowest) * 10) / 10,
      };
    })
    .sort((a, b) => (b.highest ?? -1) - (a.highest ?? -1)
                    || a.name.localeCompare(b.name));
}
