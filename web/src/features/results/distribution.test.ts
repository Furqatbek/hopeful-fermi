import { describe, expect, it } from "vitest";

import { BANDS, distribution, mean, reported } from "./distribution";

const counts = (bands: number[]) =>
  Object.fromEntries(
    distribution(bands).filter((b) => b.count > 0).map((b) => [b.band, b.count]),
  );

describe("reported", () => {
  it("rounds down to the half band, never up", () => {
    expect(reported(6.4)).toBe(6);
    expect(reported(6.9)).toBe(6.5);
    expect(reported(6.5)).toBe(6.5);
    expect(reported(7)).toBe(7);
  });

  it("does not invent a band nobody was awarded", () => {
    // The failure this guards: nearest-rounding puts a 6.4 in the 6.5 column,
    // and a teacher reads it as a student who scored half a band higher than
    // their report form says.
    expect(reported(6.4)).not.toBe(6.5);
  });
});

describe("distribution", () => {
  it("keeps every band on the scale, including the empty ones", () => {
    expect(distribution([6]).map((b) => b.band)).toEqual([...BANDS]);
  });

  it("counts the class the probe measured", () => {
    expect(counts([7.5, 6, 6, 4.5])).toEqual({ 4.5: 1, 6: 2, 7.5: 1 });
  });

  it("shows a spread the mean hides", () => {
    // Both classes average 6.0. Only one of them needs the same lesson twice.
    const flat = [6, 6, 6, 6];
    const split = [4.5, 4.5, 7.5, 7.5];
    expect(mean(flat)).toBe(mean(split));
    expect(counts(flat)).not.toEqual(counts(split));
  });

  it("folds a band below the scale into the lowest bucket rather than dropping it", () => {
    const total = distribution([3, 3.5, 6]).reduce((sum, b) => sum + b.count, 0);
    expect(total).toBe(3);
    expect(counts([3, 3.5, 6])).toEqual({ 4: 2, 6: 1 });
  });

  it("folds a 9-and-above into the top bucket", () => {
    expect(counts([9])).toEqual({ 9: 1 });
  });

  it("is empty for a class nobody has scored", () => {
    expect(distribution([]).every((b) => b.count === 0)).toBe(true);
  });
});

describe("mean", () => {
  it("is null with nothing scored, not zero", () => {
    // Zero would render as a class that sat the paper and failed it.
    expect(mean([])).toBeNull();
  });

  it("rounds to one decimal", () => {
    expect(mean([6, 6.5, 7])).toBe(6.5);
    expect(mean([6, 6, 7])).toBe(6.3);
  });
});
