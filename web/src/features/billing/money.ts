/**
 * Amounts, in tiyin.
 *
 * **Every amount crossing this API is an integer number of tiyin**, because that
 * is what Click and Payme transact in. 100 tiyin = 1 so'm. The catalogue's
 * `amount_minor`, the order's `amount_minor`, and the amount a provider echoes
 * back in a callback are all the same integer — `payme.CheckPerformTransaction`
 * refuses the transaction outright when `params.amount` does not equal
 * `orders.amount_minor` — and a decimal round trip through any one of them is how
 * money goes missing.
 *
 * So this module formats, and it deliberately does NOT parse. There is no
 * `parseSom("12 000,50")` here: the screen buys a listed product at its listed
 * price and never sends an amount of its own, so the only amount that exists is
 * the integer the server gave us. `parseQuantity` is the one exception, because a
 * seat count is the single number this screen takes from a keyboard — and it
 * multiplies a price, which earns it the same treatment as an amount.
 */

/** Uzbek prices run to millions of so'm, so grouping is not cosmetic: a centre
 *  admin has to tell 1 200 000 from 12 000 000 at a glance. A non-breaking space
 *  rather than a plain one so a price never wraps across two lines mid-number. */
export const GROUP_SEPARATOR = "\u00A0";

/** Uzbek writes the fraction after a comma, and this is the fraction of a so'm. */
export const DECIMAL_SEPARATOR = ",";

const TIYIN_PER_SOM = 100;

/**
 * What an amount we cannot render honestly is shown as.
 *
 * Reached when the value is not a whole number of tiyin — which means precision
 * has already been lost somewhere upstream. Rounding it to something plausible
 * would hide exactly the defect this module exists to prevent, and rendering `0`
 * would be a wrong price rather than a missing one.
 */
export const UNRENDERABLE = "—";

function group(digits: string): string {
  // Right to left, in slices: the grouping is anchored at the decimal point, not
  // at the start of the string, so 12345 is 12 345 and not 123 45.
  const parts: string[] = [];
  for (let end = digits.length; end > 0; end -= 3) {
    parts.unshift(digits.slice(Math.max(0, end - 3), end));
  }
  return parts.join(GROUP_SEPARATOR);
}

/**
 * Tiyin as a so'm figure, without a unit.
 *
 * The tiyin part is shown only when it is not zero. Every price in the catalogue
 * today is a whole number of so'm, and a `,00` on all of them is noise that makes
 * the one price that is NOT whole impossible to spot.
 */
export function formatSom(amount: number): string {
  if (!Number.isSafeInteger(amount)) return UNRENDERABLE;
  // `Math.abs` before splitting, then the sign back on. `Math.trunc(-50 / 100)`
  // is -0 and `-50 % 100` is -50, so the naive version renders a 50-tiyin refund
  // as "0,-50".
  const sign = amount < 0 ? "-" : "";
  const magnitude = Math.abs(amount);
  const som = Math.trunc(magnitude / TIYIN_PER_SOM);
  const tiyin = magnitude % TIYIN_PER_SOM;
  const whole = `${sign}${group(String(som))}`;
  return tiyin === 0
    ? whole
    : `${whole}${DECIMAL_SEPARATOR}${String(tiyin).padStart(2, "0")}`;
}

/**
 * A price with its unit, for display.
 *
 * UZS only. `prices.currency` defaults to UZS and both payment handlers write
 * 'UZS' when they record a payment, so nothing else is sellable today — but the
 * column allows one, and how many minor units make a major one is not the same
 * everywhere (a yen has none). Dividing by 100 regardless is how a price gets
 * shown a hundred times wrong, so anything else is left in the minor units it
 * arrived in and labelled as such.
 */
export function formatPrice(amount: number, currency: string): string {
  if (currency === "UZS") return `${formatSom(amount)} so'm`;
  if (!Number.isSafeInteger(amount)) return `${UNRENDERABLE} ${currency}`;
  return `${group(String(amount))} ${currency} minor units`;
}

/**
 * A typed seat count, as an integer — or null when it is not one.
 *
 * Strict on purpose. `Number("1e3")` is 1000 and `Number(" 2.0 ")` is 2, and both
 * would let a keystroke that does not look like a count become one: the server
 * multiplies whatever arrives by the price and charges for it. Digits only, at
 * least one seat, and small enough that JavaScript can still count it exactly.
 */
export function parseQuantity(raw: string): number | null {
  const trimmed = raw.trim();
  if (!/^[0-9]+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  if (!Number.isSafeInteger(value) || value < 1) return null;
  return value;
}

/**
 * What the order will cost: unit price times quantity, in tiyin.
 *
 * The server computes this itself (`price.amount_minor * body.quantity`) and its
 * answer is the one that is charged; this is only what the admin is shown before
 * they commit. Integer multiplication, never a currency conversion.
 *
 * Null past `MAX_SAFE_INTEGER` because `amount_minor` is a Postgres bigint and a
 * JavaScript number is a double: beyond 2^53 the product silently stops being the
 * value it prints. No real order comes near it, and a total that is quietly wrong
 * is worse than one the screen refuses to show.
 */
export function lineTotal(unitAmount: number, quantity: number): number | null {
  if (!Number.isSafeInteger(unitAmount) || unitAmount < 0) return null;
  if (!Number.isSafeInteger(quantity) || quantity < 1) return null;
  const total = unitAmount * quantity;
  return Number.isSafeInteger(total) ? total : null;
}
