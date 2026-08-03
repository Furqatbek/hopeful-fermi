/**
 * Putting the moderation queue into the order a moderator has to work it in.
 *
 * Pure and separate from the screen because it is not presentation: it corrects
 * the order the server sends, and that correction is a claim about the backend
 * that ought to be readable and testable on its own.
 *
 * **`GET /admin/reports` orders by `priority DESC`, and `priority` is a `text`
 * column.** So PostgreSQL sorts it alphabetically and descending gives
 * `normal`, `high`, `critical` — the queue hands the console its least urgent
 * reports first and its critical ones last. `critical` is set for a report that
 * involves a minor together with grooming or sexual content; those are the rows
 * that arrive at the bottom of the page, and on a full page they do not arrive
 * at all. Verified against the running database, not assumed:
 *
 *     SELECT p FROM (VALUES ('normal'),('high'),('critical')) t(p) ORDER BY p DESC
 *     -> normal, high, critical
 *
 * The partial index behind the queue is declared the same way
 * (`safety_reports_queue_idx (priority DESC, created_at)`), so the storage and
 * the query agree with each other and both disagree with the intent. Reordering
 * here is a workaround; the fix is in the backend and is reported, not made.
 *
 * Within a priority the OLDEST is first. A moderation queue's failure is a
 * report nobody looked at, and the one that has waited longest is the one
 * closest to that.
 */

/** Highest first. Anything the server invents later sorts last rather than
 *  silently at the top. */
const PRIORITY_ORDER = ["critical", "high", "normal"];

/** Statuses that still need somebody. `actioned` and `dismissed` are finished;
 *  the rest are a report waiting. */
const UNATTENDED = ["new", "triage", "investigating"];

export interface Reviewable {
  status?: string | undefined;
  priority?: string | undefined;
  created_at?: string | undefined;
}

export function priorityRank(priority: string | undefined): number {
  const at = PRIORITY_ORDER.indexOf(priority ?? "");
  return at === -1 ? PRIORITY_ORDER.length : at;
}

/** Whether this report still needs a moderator. */
export function isUnattended(report: Reviewable): boolean {
  return UNATTENDED.includes(report.status ?? "");
}

/** A timestamp as milliseconds, or `null` when it is missing or unparseable —
 *  never `0`, which would sort an undated row to 1970 and to the top of the
 *  oldest-first order it has no claim to. */
function filedAt(report: Reviewable): number | null {
  if (!report.created_at) return null;
  const at = Date.parse(report.created_at);
  return Number.isNaN(at) ? null : at;
}

/**
 * The order to work the queue in: most urgent first, then longest waiting.
 *
 * A copy, because the array belongs to the query cache and sorting it in place
 * mutates what every other reader of that cache entry sees.
 */
export function forReview<T extends Reviewable>(reports: readonly T[]): T[] {
  return [...reports].sort((a, b) => {
    const byPriority = priorityRank(a.priority) - priorityRank(b.priority);
    if (byPriority !== 0) return byPriority;
    const first = filedAt(a);
    const second = filedAt(b);
    if (first === null || second === null) return first === null ? 1 : -1;
    return first - second;
  });
}

/** How many of these still need somebody. The number the screen leads with. */
export function unattendedCount(reports: readonly Reviewable[]): number {
  return reports.filter(isUnattended).length;
}

/**
 * How long a report has been waiting, in words.
 *
 * The response SLA is a promise about elapsed time and the row carries only a
 * timestamp, so the age is the number a moderator is actually working against.
 * Coarse on purpose: nobody triages on the difference between 61 and 63 minutes,
 * and a precise number invites reading it as a countdown it is not.
 */
export function waited(createdAt: string | undefined, now: number): string {
  if (!createdAt) return "unknown";
  const at = Date.parse(createdAt);
  if (Number.isNaN(at)) return "unknown";
  const minutes = Math.floor((now - at) / 60_000);
  // A clock a few seconds behind the server's should not read as the future.
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.floor(hours / 24)} days`;
}
