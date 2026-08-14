/**
 * The shared list a matching or word-bank set chooses from, without the author
 * assigning the letters.
 *
 * `option_bank` is `[{id, text}]` — `A`, `B`, `C` beside the headings a student
 * picks between. The console asked for it as free text in the shape
 * `A = Living near water`, one per line, so a teacher typed the identifier for
 * every row by hand and owned every consequence of getting it wrong: a repeated
 * `B`, a skipped `C`, a stray `=` inside a heading.
 *
 * None of that is the author's job. The identifier is POSITION — first heading
 * is the first identifier — so it is assigned here, and the author writes only
 * the words a student reads.
 *
 * Real IELTS numbers matching-headings in lower-case roman and everything else
 * in capitals, which is why the style is a choice rather than a constant. The
 * registry agrees: `matching_headings` declares `roman_numeral_list` while the
 * other four bank types declare `option_list`. A group carries no type, so the
 * author picks — but the default is the common case.
 *
 * Pure, and tested without a browser, because this decides what the SERVER
 * stores against a paper that people will sit.
 */

export type BankStyle = "letters" | "roman";

export type Option = { id: string; text: string };

const ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
               "xi", "xii", "xiii", "xiv", "xv", "xvi"];

/** The identifier for the nth option, one-based in the reader's terms. */
export function identifier(index: number, style: BankStyle): string {
  if (style === "roman") return ROMAN[index] ?? `x${index + 1}`;
  // A–Z then AA, AB… — a bank that long is already a mistake, but running out
  // of letters silently would be a worse one.
  const letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
  if (index < letters.length) return letters[index]!;
  const first = letters[Math.floor(index / letters.length) - 1] ?? "Z";
  return `${first}${letters[index % letters.length]}`;
}

/**
 * An identifier the author may have pasted in, so it can be stripped.
 *
 * Somebody bringing a list off a real paper, or out of this product's own
 * export, has the letters already attached. Leaving them in would produce
 * "A — A. Living near water"; refusing the paste would be worse. So a leading
 * identifier in any of the shapes people actually write is removed, and the
 * position decides the real one.
 */
const ROMAN_ID = "i{1,3}|iv|ix|vi{0,3}|xi{0,3}|xiv|xv|xvi|v|x";
/** A SINGLE letter or a roman numeral, then a separator. Both halves matter.
 *
 *  It was `[A-Za-z]{1,3}` and a separator that could be absent, which deleted
 *  every heading of one to three letters — "War", "Sea", "Oil", "Tax" — from a
 *  bank without a word about it, and turned "Tax: who paid" into "who paid".
 *  Caught by a test, not by reading. An identifier is one letter or a roman
 *  numeral; anything longer is a word somebody meant. */
const LEADING_ID = new RegExp(`^\\s*(?:[A-Za-z]|${ROMAN_ID})\\s*[.)=:—–-]\\s+`, "i");

/** Text that is ONLY an identifier — "B." on its own is a dangling row, not a
 *  heading called B. The separator is REQUIRED for the same reason as above. */
const BARE_ID = new RegExp(`^\\s*(?:[A-Za-z]|${ROMAN_ID})\\s*[.)=:—–-]\\s*$`, "i");

export function stripIdentifier(line: string): string {
  const stripped = line.replace(LEADING_ID, "").trim();
  // Only strip when something survives: "A." on its own is a typo, not a
  // heading, and "Apples" must not lose its first letter to a greedy pattern.
  return stripped || line.trim();
}

/**
 * One option per line, identifiers assigned by position.
 *
 * A textarea rather than a row of inputs quite deliberately: the author has a
 * list of eight headings in another window, and pasting eight lines at once is
 * the whole task. Eight boxes to tab through is the slow version of the same
 * thing.
 */
export function parseBank(text: string, style: BankStyle): Option[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !BARE_ID.test(line))
    .map((line) => stripIdentifier(line))
    .filter(Boolean)
    .map((body, index) => ({ id: identifier(index, style), text: body }));
}

/** Back to editable text — the words only, since the identifiers are derived. */
export function formatBank(options: readonly Option[] | undefined): string {
  return (options ?? []).map((option) => option.text).join("\n");
}

/**
 * Which style an existing bank was saved in, so opening one does not silently
 * renumber it from roman to letters.
 */
export function styleOf(options: readonly Option[] | undefined): BankStyle {
  const first = options?.[0]?.id ?? "";
  return /^[ivx]+$/.test(first) ? "roman" : "letters";
}

/** What is wrong with the bank, in words, or null. Advisory only: the server
 *  enforces `option_bank_at_least`, and this is the version that arrives before
 *  a round trip. */
export function bankProblem(options: readonly Option[], min = 0,
                            max = 0): string | null {
  if (options.length === 0) return null;              // empty is a valid "no bank"
  if (min && options.length < min) {
    return `A bank needs at least ${min} options — there ${
      options.length === 1 ? "is 1" : `are ${options.length}`}.`;
  }
  if (max && options.length > max) {
    return `A bank holds at most ${max} options — there are ${options.length}.`;
  }
  const seen = new Set<string>();
  for (const option of options) {
    const key = option.text.toLowerCase();
    if (seen.has(key)) {
      return `"${option.text}" is in the list twice. A student choosing between `
        + "two identical options has no right answer.";
    }
    seen.add(key);
  }
  return null;
}
