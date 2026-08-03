/**
 * The URL name of an organization, and why the form cannot just derive one.
 *
 * `OrgCreate.slug` is pinned server-side to `^[a-z0-9-]{3,40}$` and there is no
 * endpoint that changes it afterwards — `OrgUpdate` carries `name`,
 * `contact_phone` and `settings` and nothing else. So the value typed at
 * creation is permanent, which makes silently inventing one the wrong move.
 *
 * The reason a suggestion is not enough on its own: centre names here are
 * routinely Cyrillic ("Тошкент Прэп") or carry the Uzbek okina ("Toshkent Oʻquv
 * Markazi"). Stripping everything outside `[a-z0-9-]` turns the first into an
 * empty string and the second into something the admin did not intend. An empty
 * suggestion has to read as "type one", not as a slug.
 */

/** The server's pattern, spelled once. */
export const SLUG_PATTERN = /^[a-z0-9-]{3,40}$/;

const MAX = 40;

/**
 * A slug proposed from the organization's name.
 *
 * Returns "" when nothing usable survives, which the caller must render as a
 * prompt rather than submit. Accents are decomposed and their combining marks
 * dropped so "Farg'ona" and "Fargʻona" both reach "fargona"; anything with no
 * ASCII letters or digits at all — Cyrillic, Arabic — has nothing to decompose
 * into and correctly comes back empty.
 */
export function suggestSlug(name: string): string {
  const stripped = name
    .normalize("NFD")
    // Combining marks, i.e. the accents NFD just separated out.
    .replace(/[̀-ͯ]/g, "")
    // The okina, DELETED rather than turned into a separator like every other
    // non-alphanumeric. `oʻ` and `gʻ` are single letters of Uzbek Latin, so
    // "Fargʻona" is one word: the general rule below would cut it into
    // "farg-ona" and hand the centre a permanent web address with a hyphen in
    // the middle of its name. All four spellings staff actually type — the
    // modifier letters, the curly quotes, and a plain apostrophe.
    .replace(/[ʻʼ‘’']/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (stripped.length < 3) return "";
  // Trimmed to the limit and then re-trimmed for a trailing hyphen: cutting a
  // 41-character slug mid-word can land exactly on a separator, and "-" at the
  // end fails the same pattern the cut was meant to satisfy.
  return stripped.slice(0, MAX).replace(/-+$/g, "");
}

/**
 * What is wrong with a slug, in words, or null when it is acceptable.
 *
 * Separate from the pattern test because "invalid" is not a useful thing to tell
 * somebody typing a name: the three ways to fail have three different fixes, and
 * the length one is the only one that is not obvious from looking at the box.
 */
export function slugProblem(slug: string): string | null {
  if (slug.length === 0) {
    return "Give a short name for the web address. Letters a-z, digits and "
      + "hyphens only.";
  }
  if (slug.length < 3) return "Use at least three characters.";
  if (slug.length > MAX) return `Use at most ${MAX} characters.`;
  if (!SLUG_PATTERN.test(slug)) {
    return "Use lower-case letters a-z, digits and hyphens only — no spaces, "
      + "no capitals, no other alphabets.";
  }
  return null;
}
