/**
 * The five decisions, and the clock the claimant is watching.
 *
 * `PATCH /admin/takedowns/{xid}` validates `status` against a pattern that is
 * the takedown status list minus `received` — filing sets `received` and no
 * decision restores it — so `Decision` is derived by exactly that subtraction
 * rather than by retyping five strings that a sixth would silently outgrow.
 *
 * Two of the five look like the same act and are not. `upheld` says the claim is
 * good and the material stays hidden; `counter_noticed` says the centre has
 * answered the claim and the dispute is now between the two parties, which is a
 * different record to leave behind. `withdrawn` is the claimant's own doing.
 * A queue row is the evidence of what was decided, so the words have to keep
 * meaning what they mean.
 */

import type { components } from "../../api/schema";

type Status = NonNullable<components["schemas"]["Takedown"]["status"]>;

export type Decision = Exclude<Status, "received">;

const DECISIONS: Record<Decision, { label: string; help: string }> = {
  reviewing: {
    label: "Reviewing",
    help: "Taken up, not yet decided. The material stays hidden and the request "
      + "stays in this queue.",
  },
  upheld: {
    label: "Upheld",
    help: "The claim is good. The material stays hidden and the centre is at "
      + "fault.",
  },
  rejected: {
    label: "Rejected",
    help: "The claim does not stand. Say why — this note is the whole answer if "
      + "the claimant comes back.",
  },
  counter_noticed: {
    label: "Counter-noticed",
    help: "The centre has answered the claim. The dispute is now between the two "
      + "parties, not ours to settle.",
  },
  withdrawn: {
    label: "Withdrawn",
    help: "The claimant took it back.",
  },
};

/** In the order the form offers them: the one that keeps the request open
 *  first, then the four that close it. */
export const DECISION_LIST = (Object.keys(DECISIONS) as Decision[]).map(
  (value) => ({ value, ...DECISIONS[value] }),
);

/**
 * Whether a request is still in the open queue.
 *
 * `received` and `reviewing`, matching `takedown_requests_queue_idx` — the
 * partial index the default listing runs on. `reviewing` is a decision that does
 * NOT remove the row, and a screen that treated every decision as final would
 * tell an admin their request had left a queue it is still sitting in.
 */
export function isOpen(status: string | undefined | null): boolean {
  return status === "received" || status === "reviewing";
}

/**
 * Whole days a request has been waiting.
 *
 * The queue is oldest-first on purpose and the clock a rights holder cares about
 * started when they filed, so the age is worth a column of its own — the sort
 * order alone does not say whether the top row arrived yesterday or in March.
 *
 * `null`, not zero, for a missing or unparseable date. Zero is a real answer
 * here (filed today) and using it for "we do not know" would show the oldest
 * possible complaint as the freshest.
 */
export function waitingDays(
  receivedAt: string | undefined | null,
  now: Date,
): number | null {
  if (!receivedAt) return null;
  const filed = new Date(receivedAt).getTime();
  if (Number.isNaN(filed)) return null;
  return Math.max(0, Math.floor((now.getTime() - filed) / 86_400_000));
}
