/**
 * Which of a centre's seat licences actually grant anything.
 *
 * **Only one does, per feature, and the panel showed them all as equals.** The
 * server resolves a centre's seat licence by taking a SINGLE row — live first,
 * then newest — because a centre that renews has two and last year's exhausted
 * one must not be the answer. So a second live licence for the same feature is
 * not extra seats. It grants nothing at all.
 *
 * Found by rendering the grant form, not by reading it: granting twice takes two
 * clicks, and the result was two live rows listed one above the other, reading
 * as four seats to anybody who added them up. An operator would tell a centre it
 * had four and the centre would be refused on the second.
 *
 * Per FEATURE, because that is the key the lookup resolves on — two licences for
 * different features are two licences; two for the same one are one.
 *
 * Pure, and here rather than inline in the component, so the rule can be tested
 * without a DOM. That is the same reason `nav.ts` and `slug.ts` are separate.
 */

export type SeatRow = {
  feature: string;
  source_kind?: string;
  revoked_at?: string | null;
};

/** The features on which this centre holds more than one live seat licence. */
export function shadowedFeatures(rows: readonly SeatRow[]): string[] {
  const live = rows.filter(
    (row) => row.source_kind === "seat" && !row.revoked_at);
  const counts = new Map<string, number>();
  for (const row of live) {
    counts.set(row.feature, (counts.get(row.feature) ?? 0) + 1);
  }
  return [...counts].filter(([, n]) => n > 1).map(([feature]) => feature);
}

/** Does this centre hold any live seat licence at all? */
export function hasLiveSeatLicence(rows: readonly SeatRow[]): boolean {
  return rows.some((row) => row.source_kind === "seat" && !row.revoked_at);
}

/**
 * What the "Left" column should say.
 *
 * **A seat licence's quantity is a SIZE, not a balance.** `remaining` is
 * `quantity - consumed`, and nothing consumes a seat — a seat is spent by being
 * ASSIGNED, which lives in `seat_assignments` and is not in this payload. So the
 * column rendered "3 of 3" for a centre with every seat taken, which reads as
 * three free and is the opposite of true. How many are in use belongs to the
 * centre's own seats screen; what this panel decides is how many exist.
 */
export function quantityLabel(
  row: { source_kind?: string; quantity?: number | null; remaining?: number | null },
): string {
  if (row.source_kind === "seat") {
    const seats = row.quantity ?? 0;
    return `${seats} seat${seats === 1 ? "" : "s"}`;
  }
  if (row.quantity === null || row.quantity === undefined) return "unlimited";
  return `${row.remaining ?? 0} of ${row.quantity}`;
}
