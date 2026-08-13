/**
 * What a student may actually do with a piece of assigned work.
 *
 * Four things gate an assignment and all four are visible in the payload, so
 * this is decided here rather than by starting an attempt and reading the
 * refusal. A "Start" button that always fails is worse than no button: the
 * student cannot tell a closed window from a broken product.
 *
 * The server decides for real, always — `POST /attempts` enforces every one of
 * these again. This is what to render, never what is permitted.
 *
 * ── on which clock ──────────────────────────────────────────────────────────
 *
 * `now` is a parameter and the caller passes the device clock. That is
 * deliberate and it is NOT the same decision as the exam countdown, which takes
 * a server delta and never trusts the device (`exam/clock.ts`). The difference
 * is what an error costs: a handset an hour fast shows "closes in 2 days"
 * instead of "3", which is a cosmetic wrong, while the same drift on a section
 * timer silently steals the end of somebody's exam. Precision where it is worth
 * paying for.
 */

export type Assignment = {
  xid: string;
  test_title?: string;
  opens_at: string;
  closes_at: string;
  mode: "exam" | "practice";
  max_attempts?: number;
  my_attempts_used?: number;
  time_limit_seconds?: number | null;
};

export type State =
  /** Open, and the student has an attempt left. */
  | "startable"
  /** The window has not opened yet. */
  | "upcoming"
  /** The window has closed. */
  | "closed"
  /** Open, but every permitted attempt is spent. */
  | "exhausted";

export function stateOf(a: Assignment, now = Date.now()): State {
  const opens = Date.parse(a.opens_at);
  const closes = Date.parse(a.closes_at);

  // Order matters. An assignment can be BOTH closed and exhausted, and "closed"
  // is the more useful thing to say — a student who used their attempts can do
  // nothing about either, but one who still had an attempt and ran out of time
  // is being told something they can act on next time.
  if (now < opens) return "upcoming";
  if (now >= closes) return "closed";

  const used = a.my_attempts_used ?? 0;
  const max = a.max_attempts ?? 1;
  return used >= max ? "exhausted" : "startable";
}

export function attemptsLeft(a: Assignment): number {
  return Math.max(0, (a.max_attempts ?? 1) - (a.my_attempts_used ?? 0));
}

/**
 * "closes in 3 days" / "closes in 4 hours" / "opens Tuesday" / "closed".
 *
 * Coarse on purpose. A deadline three days out shown to the minute invites
 * somebody to plan around a number their own clock cannot support, and the
 * exact instant is enforced server-side regardless.
 */
export function deadlineText(a: Assignment, now = Date.now()): string {
  const state = stateOf(a, now);
  if (state === "closed") return "closed";

  const target = state === "upcoming" ? Date.parse(a.opens_at) : Date.parse(a.closes_at);
  const verb = state === "upcoming" ? "opens" : "closes";
  const ms = target - now;

  const minutes = Math.round(ms / 60_000);
  if (minutes < 1) return `${verb} now`;
  if (minutes < 60) return `${verb} in ${minutes} min`;

  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${verb} in ${hours} ${hours === 1 ? "hour" : "hours"}`;

  const days = Math.round(hours / 24);
  return `${verb} in ${days} ${days === 1 ? "day" : "days"}`;
}

/** `time_limit_seconds` as "60 min", or null when the assignment is untimed. */
export function limitText(a: Assignment): string | null {
  if (!a.time_limit_seconds) return null;
  const minutes = Math.round(a.time_limit_seconds / 60);
  return `${minutes} min`;
}

/**
 * Startable work first, then what is coming, then what can no longer be done.
 *
 * Within a group, by the deadline that matters for that group: the soonest
 * closing first among open work, because that is the one at risk, and the
 * soonest opening first among upcoming.
 */
const RANK: Record<State, number> = { startable: 0, upcoming: 1, exhausted: 2, closed: 3 };

export function ordered(list: readonly Assignment[], now = Date.now()): Assignment[] {
  return [...list].sort((a, b) => {
    const byState = RANK[stateOf(a, now)] - RANK[stateOf(b, now)];
    if (byState !== 0) return byState;
    const key = (x: Assignment) =>
      stateOf(x, now) === "upcoming" ? Date.parse(x.opens_at) : Date.parse(x.closes_at);
    return key(a) - key(b);
  });
}
