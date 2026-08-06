/**
 * Ordering the burn watch list.
 *
 * There is no listing endpoint for exposure. `GET /questions` DECLARES
 * `burn_score` on every row of the bank and `question_dto` hardcodes it to
 * `null`, so the only source of a real number is `GET /questions/{xid}/exposure`
 * — one request per item, for one item.
 *
 * That is what makes the ORDER the whole value of the screen. An author does not
 * open this to look up an item whose xid they already have; they open it to find
 * out which of their items are burning. Fetched separately and left in bank
 * order, the burning one is wherever it happens to be, and the reader stops at
 * the top of the list.
 *
 * Nothing here re-derives `recommendation`. `stats.exposure_recommendation` is
 * the one place "how burned is too burned" is decided — the exposure endpoint and
 * the competition freshness gate deliberately share it — and a second copy of
 * `0.3` and `0.7` in the console would be a third answer to that question.
 */

/** The two fields ranking needs. A narrow shape rather than `ExposureReport`, so
 *  the ordering can be tested without a fixture of eight optional fields. */
export interface Exposure {
  burn_score?: number;
  times_sat?: number;
}

/** A burn score as a whole percentage, for the bar and for the number beside it.
 *  Clamped, because a width outside 0-100% renders as a bar out of its row. */
export function burnPercent(burn: number | undefined | null): number {
  if (typeof burn !== "number" || Number.isNaN(burn)) return 0;
  return Math.round(Math.min(1, Math.max(0, burn)) * 100);
}

/**
 * Most burned first; items we could not read last.
 *
 * `exposure: null` means the per-item request has not answered yet or was
 * refused — NOT that the item is fresh. Sorting those as zero would file an
 * unknown item among the safe ones, which on a screen whose entire job is
 * "which of these has circulated" is the one wrong place to put it. They go to
 * the bottom and the row says so.
 *
 * Ties break on `times_sat`, because two items at the same score are not equally
 * spent: `burn_score` saturates, and past a few hundred sittings the difference
 * between them survives only in the count.
 */
export function rankByBurn<T extends { exposure: Exposure | null }>(
  rows: readonly T[],
): T[] {
  return [...rows].sort((a, b) => {
    if (!a.exposure || !b.exposure) {
      return (a.exposure ? 0 : 1) - (b.exposure ? 0 : 1);
    }
    const byBurn = (b.exposure.burn_score ?? 0) - (a.exposure.burn_score ?? 0);
    if (byBurn !== 0) return byBurn;
    return (b.exposure.times_sat ?? 0) - (a.exposure.times_sat ?? 0);
  });
}
