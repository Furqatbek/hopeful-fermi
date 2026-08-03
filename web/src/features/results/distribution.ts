/**
 * Turning a column of bands into the shape of a class.
 *
 * Pure, and separate from the screen, because the bucketing rule is a judgment
 * that would otherwise only be visible by staring at a chart: **a band is
 * reported by rounding DOWN to the nearest half.** A 6.4 is a 6.0 on an IELTS
 * report form, and bucketing it as 6.5 would show a teacher a grade none of
 * their students actually received.
 */

/** IELTS reports whole and half bands. Fixed buckets, so an empty band still
 *  occupies its row — a gap in the middle of a distribution is information. */
export const BANDS = [4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5, 8, 8.5, 9] as const;

export interface Bucket {
  band: number;
  count: number;
}

/** The reported band for a raw band value: down to the nearest half. */
export function reported(band: number): number {
  return Math.floor(band * 2) / 2;
}

/**
 * Bucket bands into the fixed scale.
 *
 * Anything below the lowest bucket is folded into it rather than dropped. A 3.5
 * is a real result and a student who got one is in the room; silently discarding
 * them would make the counts disagree with the roll, which is exactly the kind of
 * quiet arithmetic error that destroys trust in a report.
 */
export function distribution(bands: number[]): Bucket[] {
  // Named rather than indexed off the ends of BANDS: under
  // `noUncheckedIndexedAccess` those reads are `number | undefined`, and the
  // honest fix is to state the scale's bounds, not to assert them away.
  const lowest = 4;
  const highest = 9;
  return BANDS.map((band) => ({
    band,
    count: bands.filter((value) => {
      const at = Math.min(highest, Math.max(lowest, reported(value)));
      return at === band;
    }).length,
  }));
}

/** Mean to one decimal, or null when nothing is scored — NOT zero, which would
 *  read as a class that sat the paper and failed it. */
export function mean(bands: number[]): number | null {
  if (bands.length === 0) return null;
  return Math.round((bands.reduce((a, b) => a + b, 0) / bands.length) * 10) / 10;
}
