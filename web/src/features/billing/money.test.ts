import { describe, expect, it } from "vitest";

import {
  GROUP_SEPARATOR,
  UNRENDERABLE,
  formatPrice,
  formatSom,
  lineTotal,
  parseQuantity,
} from "./money";

/** Written out so a failure message shows which character is wrong: a plain
 *  space and a non-breaking one are indistinguishable in a diff. */
const NBSP = "\u00A0";

describe("formatSom", () => {
  it("uses a non-breaking space so a price cannot wrap mid-number", () => {
    expect(GROUP_SEPARATOR).toBe(NBSP);
  });

  it("is a bare zero, not an empty string", () => {
    expect(formatSom(0)).toBe("0");
  });

  it("keeps an amount under one so'm as a fraction of one", () => {
    // 100 tiyin = 1 so'm. Dropping the fraction here would print 1 tiyin and
    // 99 tiyin as the same "0".
    expect(formatSom(1)).toBe("0,01");
    expect(formatSom(50)).toBe("0,50");
    expect(formatSom(99)).toBe("0,99");
  });

  it("is exactly one so'm at a hundred tiyin", () => {
    expect(formatSom(100)).toBe("1");
  });

  it("shows the tiyin when the amount is not a whole number of so'm", () => {
    expect(formatSom(120_050)).toBe(`1${NBSP}200,50`);
    // Padded: 5 tiyin is five hundredths of a so'm, not five tenths.
    expect(formatSom(120_005)).toBe(`1${NBSP}200,05`);
  });

  it("hides the tiyin when there are none", () => {
    // Every catalogue price today is a whole number of so'm. A ",00" on all of
    // them is noise that makes the one price that is NOT whole invisible.
    expect(formatSom(120_000)).toBe(`1${NBSP}200`);
  });

  it("groups thousands, because Uzbek prices run to millions of so'm", () => {
    expect(formatSom(100)).toBe("1");
    expect(formatSom(99_900)).toBe("999");
    expect(formatSom(100_000)).toBe(`1${NBSP}000`);
    expect(formatSom(1_200_000)).toBe(`12${NBSP}000`);
    expect(formatSom(12_000_000)).toBe(`120${NBSP}000`);
    // The pair a centre admin must be able to tell apart at a glance: a seat
    // bundle at 1 200 000 so'm and one at 12 000 000.
    expect(formatSom(120_000_000)).toBe(`1${NBSP}200${NBSP}000`);
    expect(formatSom(1_200_000_000)).toBe(`12${NBSP}000${NBSP}000`);
  });

  it("groups from the decimal point, not from the start", () => {
    expect(formatSom(1_234_567_800)).toBe(`12${NBSP}345${NBSP}678`);
  });

  it("renders a refund with the sign on the whole figure", () => {
    // `Math.trunc(-50 / 100)` is -0 and `-50 % 100` is -50, so the naive split
    // prints a 50-tiyin refund as "0,-50".
    expect(formatSom(-50)).toBe("-0,50");
    expect(formatSom(-120_050)).toBe(`-1${NBSP}200,50`);
  });

  it("refuses to round a fractional tiyin into a plausible price", () => {
    // A fraction of a tiyin means precision was already lost upstream. Printing
    // "12 000" for it would hide exactly that.
    expect(formatSom(1_200_000.5)).toBe(UNRENDERABLE);
    expect(formatSom(0.5)).toBe(UNRENDERABLE);
  });

  it("refuses a number that is not a number", () => {
    expect(formatSom(Number.NaN)).toBe(UNRENDERABLE);
    expect(formatSom(Number.POSITIVE_INFINITY)).toBe(UNRENDERABLE);
  });

  it("refuses an amount past the point a double counts exactly", () => {
    expect(formatSom(Number.MAX_SAFE_INTEGER + 2)).toBe(UNRENDERABLE);
  });
});

describe("formatPrice", () => {
  it("names the unit a centre admin reads prices in", () => {
    expect(formatPrice(120_000_000, "UZS")).toBe(`1${NBSP}200${NBSP}000 so'm`);
  });

  it("does not divide a currency whose minor units it does not know", () => {
    // A yen has no minor unit at all. Dividing by 100 regardless is how a price
    // gets shown a hundred times wrong.
    expect(formatPrice(120_000_000, "JPY")).toBe(
      `120${NBSP}000${NBSP}000 JPY minor units`,
    );
    expect(formatPrice(1_500, "USD")).toBe(`1${NBSP}500 USD minor units`);
  });

  it("still refuses a fractional amount in another currency", () => {
    expect(formatPrice(1.5, "USD")).toBe(`${UNRENDERABLE} USD`);
  });
});

describe("parseQuantity", () => {
  it("reads a plain count", () => {
    expect(parseQuantity("1")).toBe(1);
    expect(parseQuantity("25")).toBe(25);
    expect(parseQuantity(" 10 ")).toBe(10);
  });

  it("rejects an empty box rather than treating it as one seat", () => {
    expect(parseQuantity("")).toBeNull();
    expect(parseQuantity("   ")).toBeNull();
  });

  it("rejects anything that is not a count", () => {
    // `Number("1e3")` is 1000 and `Number("2.0")` is 2. The server multiplies
    // whatever arrives by the price and charges for it, so neither keystroke may
    // become a quantity.
    expect(parseQuantity("1e3")).toBeNull();
    expect(parseQuantity("2.0")).toBeNull();
    expect(parseQuantity("1.5")).toBeNull();
    expect(parseQuantity("-3")).toBeNull();
    expect(parseQuantity("+3")).toBeNull();
    expect(parseQuantity("10 seats")).toBeNull();
    expect(parseQuantity("ten")).toBeNull();
  });

  it("rejects zero seats, which is an order for nothing", () => {
    expect(parseQuantity("0")).toBeNull();
    expect(parseQuantity("00")).toBeNull();
  });

  it("rejects a count too large to be counted exactly", () => {
    expect(parseQuantity("9".repeat(20))).toBeNull();
  });
});

describe("lineTotal", () => {
  it("multiplies in tiyin", () => {
    expect(lineTotal(120_000_000, 10)).toBe(1_200_000_000);
    expect(formatPrice(lineTotal(120_000_000, 10) ?? 0, "UZS")).toBe(
      `12${NBSP}000${NBSP}000 so'm`,
    );
  });

  it("is the unit price for one", () => {
    expect(lineTotal(120_000_000, 1)).toBe(120_000_000);
  });

  it("refuses a quantity that is not a whole number of seats", () => {
    expect(lineTotal(120_000_000, 1.5)).toBeNull();
    expect(lineTotal(120_000_000, 0)).toBeNull();
    expect(lineTotal(120_000_000, -1)).toBeNull();
  });

  it("refuses a unit price that is not a whole number of tiyin", () => {
    expect(lineTotal(1_200.5, 2)).toBeNull();
    expect(lineTotal(-100, 2)).toBeNull();
  });

  it("refuses a total a double can no longer count exactly", () => {
    // Past 2^53 the product silently stops being the value it prints, and a
    // total that is quietly wrong is worse than one the screen will not show.
    expect(lineTotal(Number.MAX_SAFE_INTEGER, 2)).toBeNull();
  });
});
