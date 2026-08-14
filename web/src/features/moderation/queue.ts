/**
 * Putting the moderation queue into the order a moderator has to work it in.
 *
 * Pure and separate from the screen because it is not presentation: it corrects
 * the order the server sends, and that correction is a claim about the backend
 * that ought to be readable and testable on its own.
 *
 * `GET /admin/reports` used to order by `priority DESC` over a `text` column,
 * which sorts alphabetically: descending gave `normal`, `high`, `critical`, so
 * the queue handed the console its least urgent reports first and its critical
 * ones last — and `critical` is set for a report involving a minor together
 * with grooming or sexual content. On a full page those rows did not arrive at
 * all. The server now orders by an explicit rank and the index is declared to
 * match (migration 0027).
 *
 * This still sorts, and deliberately. The console must not be the only thing
 * standing between a moderator and that ordering, but it must not silently
 * depend on the server either: two sorts that agree cost nothing, and if the
 * backend order ever regresses the screen keeps working while the backend test
 * fails loudly. Within a priority the OLDEST is first — a moderation queue's
 * failure is a report nobody looked at, and the one that has waited longest is
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


/** The category as a moderator reads it, not as the column stores it.
 *
 * Seven of the eight enum values are single words and rendered fine; the eighth
 * is `sexual_content`, and it went to screen with the underscore in it —
 * database text in the column a moderator triages by, one row above `grooming`
 * and `harassment` in plain English. Underscores to spaces and a leading
 * capital, rather than a lookup table: the values are already the words, and a
 * table would need editing every time the enum grows while this would not. An
 * unknown value still renders as itself, which is the right failure — a
 * category nobody can read is better than a category silently dropped.
 *
 * `undefined` is in the signature because the generated type says the field is
 * optional, and the cell it replaces rendered nothing for it. Narrowing that to
 * `string` here would move the decision to the call site, where it would be
 * made with `!` and stop being a decision. */
export function categoryLabel(category: string | undefined): string {
  if (!category) return "";
  const words = category.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
