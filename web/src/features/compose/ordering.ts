/**
 * Reordering, as pure functions, because ordering is where this product has
 * already been bitten.
 *
 * `tests_authoring.py` carried a section move that "500'd in one direction and
 * silently left a hole in the section sequence in the other" — and a hole is not
 * cosmetic: it is a student who cannot enter the next section of a timed exam.
 * So the arithmetic lives here, out of the component, where a test can drive it
 * without a DOM or a drag.
 *
 * The two levels use different endpoints, and the difference matters:
 *
 *   * **Groups within a section** — `POST /sections/{xid}/reorder` takes the
 *     WHOLE ordered list of placement xids. The server assigns positions from
 *     it, so a hole is unrepresentable and the call is idempotent. Send the list.
 *
 *   * **Sections within a version** — `PATCH /sections/{xid}` with a new
 *     `position`, one section at a time. That is the shape with the history, and
 *     it is why `sectionMoves` below returns the SEQUENCE of single-section
 *     moves needed rather than pretending one call does it.
 */

export type Ordered = { xid: string; position?: number };

/** Move `from` to `to` in a copy. The workhorse behind both levels. */
export function reorder<T>(items: readonly T[], from: number, to: number): T[] {
  const next = [...items];
  const [moved] = next.splice(from, 1);
  if (moved === undefined) return next;
  next.splice(to, 0, moved);
  return next;
}

/**
 * The section PATCHes needed to get from the current order to `desired`.
 *
 * Returns only sections whose position actually changes, and returns them in an
 * order that is safe to apply one at a time. `_move_section` on the server
 * resequences its siblings around the section being moved, so applying these
 * sequentially converges — but sending a no-op PATCH for every section would
 * make a five-section reorder five writes to the audit log for one intent.
 *
 * Positions are 1-based, matching `SectionCreate.position: { minimum: 1 }`.
 */
export function sectionMoves(
  desired: readonly Ordered[],
): { xid: string; position: number }[] {
  return desired
    .map((section, index) => ({ xid: section.xid, position: index + 1 }))
    .filter((next, index) => desired[index]?.position !== next.position);
}

/**
 * True when `positions` is exactly 1..n with nothing missing or repeated.
 *
 * The invariant a hole breaks. Checked after every reorder so a bad server
 * response is visible on the screen that caused it rather than to a student
 * mid-exam — the failure this whole module is shaped around.
 */
export function isContiguous(positions: readonly (number | undefined)[]): boolean {
  const sorted = [...positions].filter((p): p is number => p !== undefined).sort((a, b) => a - b);
  if (sorted.length !== positions.length) return false;
  return sorted.every((position, index) => position === index + 1);
}
