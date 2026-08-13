/**
 * The countdown, driven by the SERVER.
 *
 * `docs/design/0013-student-exam-ui.md` §7.1 and ADR-0001 §5.3: the server is
 * the sole authority on time remaining, and the device clock is never trusted.
 * A cheap Android handset can be minutes out, and a student whose phone is fast
 * would lose the end of their exam to a bug they cannot see.
 *
 * So we never ask the device what time it is in absolute terms. We ask it only
 * how long *it* thinks has passed since the last server reading — a delta, which
 * is accurate even on a clock set to the wrong year — and subtract that from
 * what the server said was left.
 *
 * `performance.now()` rather than `Date.now()` for the elapsed part: it is
 * monotonic, so an NTP correction or the user changing their clock mid-exam
 * cannot make time jump backwards or leap forwards.
 */

export type ServerReading = {
  /** `server_now` from the API, ISO-8601 UTC. */
  serverNow: string;
  /** `expires_at` from the API, ISO-8601 UTC. */
  expiresAt: string;
};

export type Clock = {
  /** Milliseconds left at the moment this was taken. Never negative. */
  remainingMs: number;
  /** Monotonic stamp of when the reading was taken. */
  takenAt: number;
};

/**
 * Fold a server response into a clock. Call this on EVERY response that carries
 * `server_now` — the answers endpoint documents itself as "the response is the
 * clock sync", so a student saving answers is continuously re-syncing for free.
 */
export function sync(reading: ServerReading, now = performance.now()): Clock {
  const remaining = Date.parse(reading.expiresAt) - Date.parse(reading.serverNow);
  return {
    // Clamp at zero. A negative remaining is not a countdown running backwards,
    // it is an expired attempt, and every caller wants the same answer: none left.
    remainingMs: Math.max(0, remaining),
    takenAt: now,
  };
}

/** Milliseconds left right now, extrapolated from the last sync. */
export function remaining(clock: Clock, now = performance.now()): number {
  return Math.max(0, clock.remainingMs - (now - clock.takenAt));
}

/**
 * `mm:ss`, which is what the real test shows.
 *
 * Rounds UP, so the display reaches 0:00 exactly when time is actually gone. A
 * flooring clock shows 0:00 for a whole second while the student can still type,
 * and that second reads as the exam having stolen time from them.
 */
export function format(ms: number): string {
  const total = Math.ceil(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/**
 * The warning state. Verified against the real client: the timer "starts to
 * flash in the last 10 minutes and 5 minutes before reading and writing tests
 * end" (docs/design/0013 §9).
 *
 * Two distinct thresholds rather than one, because that is what the real test
 * does and the whole point is that the rhythm is familiar.
 */
export type Urgency = "normal" | "ten-minutes" | "five-minutes";

export function urgency(ms: number): Urgency {
  const minutes = ms / 60_000;
  if (minutes <= 5) return "five-minutes";
  if (minutes <= 10) return "ten-minutes";
  return "normal";
}
