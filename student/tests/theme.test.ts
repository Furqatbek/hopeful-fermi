/**
 * Every theme must define every semantic colour.
 *
 * `inverse` and `yellow-on-black` overrode eight tokens and left `--warn` and
 * `--danger` behind, so the light theme's dark orange and dark red carried onto
 * a near-black page. Measured against `--pane`: 3.71 and 2.83 in `inverse`,
 * 3.94 and 3.01 in `yellow-on-black`, against the 4.5:1 the rest of this sheet
 * clears comfortably.
 *
 * Those two are not decoration. `--warn` is the exam timer at ten minutes left
 * and the word limit that decides whether an answer is marked wrong;
 * `--danger` is the timer at FIVE minutes and the "incorrect" verdict on the
 * review screen. The least readable text on the page was the text that costs a
 * candidate marks — and `yellow-on-black` exists precisely for candidates who
 * cannot read low contrast.
 *
 * A source assertion rather than a rendered one: this suite has no DOM, and a
 * missing override is visible in the stylesheet. The contrast itself was
 * measured in a real browser, in a real exam, across all four themes; this
 * stops a NEW theme, or a new token, from reintroducing the same hole.
 *
 * Outside `src/`, for the same reason `vite.config.ts` is: the app tsconfig
 * targets a browser and does not carry node types, so a `node:fs` import inside
 * `src` fails `npm run build` even though vitest runs it happily.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const css = readFileSync(fileURLToPath(new URL("../src/styles.css", import.meta.url)), "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "");

/** The tokens `:root` defines, which every theme therefore has to answer for. */
function tokensIn(block: string): Set<string> {
  return new Set([...block.matchAll(/(--[a-z][a-z0-9-]*)\s*:/g)].map((m) => m[1]!));
}

function blockFor(selector: string): string {
  const at = css.indexOf(selector);
  if (at === -1) return "";
  return css.slice(at, css.indexOf("}", at));
}

/** Colours only. `--warn` and `--danger` are the pair that went missing, and
 *  they are the pair that matters most; sizes and radii are not theme-borne. */
const COLOUR = /^--(ink|paper|pane|muted|line|accent|accent-ink|warn|good|danger|highlight)$/;

describe("themes", () => {
  const base = [...tokensIn(blockFor(":root {"))].filter((t) => COLOUR.test(t));

  it("has a base palette to compare against", () => {
    // Guards the guard: a rename that emptied this would make every assertion
    // below pass by checking nothing.
    expect(base.length).toBeGreaterThanOrEqual(9);
    expect(base).toContain("--warn");
    expect(base).toContain("--danger");
  });

  for (const theme of ["inverse", "cream", "yellow-on-black"]) {
    it(`${theme} answers for every colour the base defines`, () => {
      const block = blockFor(`:root[data-theme="${theme}"]`);
      expect(block).not.toBe("");
      const defined = tokensIn(block);
      // `cream` is a light theme and legitimately inherits the light values;
      // the DARK themes cannot inherit anything a light page assumed.
      const required = theme === "cream" ? ["--paper", "--pane", "--ink"] : base;
      const missing = required.filter((token) => !defined.has(token));
      expect(missing).toEqual([]);
    });
  }
});

/**
 * Present is not the same claim as legible, and this app shipped the gap.
 *
 * The check above stops a theme from OMITTING `--warn` or `--danger`. It
 * cannot stop one from DEFINING them badly — and the base theme did, in a
 * spot the first pass never measured. `inverse` and `yellow-on-black` were
 * fixed against `--pane`; the exam timer bar (`.exam-top`) and a marking
 * verdict row (`.mark`) are both painted `--paper`, and nobody had checked the
 * DEFAULT theme's own orange against the OTHER background its own text sits
 * on. It was 4.47:1 there, and 4.17:1 once `cream` — which inherits `--warn`
 * rather than setting its own — tints paper further. Both are the timer at ten
 * minutes and the "partial" verdict: the docstring above already named these
 * as the text that costs a candidate marks, and a token can be present in
 * every theme and still be that dim.
 *
 * So this computes the actual WCAG ratio rather than trusting the swatch,
 * the same way `web/tests/styles.test.ts` does for the admin console.
 */
describe("every theme's text clears 4.5:1 against every background it sits on", () => {
  function hex(token: string, block: string): string | null {
    const m = block.match(new RegExp(`${token}:\\s*(#[0-9a-fA-F]{6})`));
    return m ? m[1]! : null;
  }

  function luminance(hexColor: string): number {
    const channels = [1, 3, 5].map((i) => parseInt(hexColor.slice(i, i + 2), 16) / 255);
    const [r, g, b] = channels.map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
  }

  function contrast(a: string, b: string): number {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi! + 0.05) / (lo! + 0.05);
  }

  const rootBlock = blockFor(":root {");

  /** A theme's own value for a token, falling back to the base — exactly how
   *  the cascade actually resolves it for `cream`, which only overrides five. */
  function resolve(token: string, themeBlock: string): string {
    const value = hex(token, themeBlock) ?? hex(token, rootBlock);
    if (!value) throw new Error(`${token} has no value in this theme or the base`);
    return value;
  }

  const THEMES = ["", "inverse", "cream", "yellow-on-black"]; // "" is the base itself

  it("finds a real value for every token in every theme, or the checks below prove nothing", () => {
    for (const theme of THEMES) {
      const block = theme ? blockFor(`:root[data-theme="${theme}"]`) : rootBlock;
      for (const token of ["ink", "muted", "accent", "warn", "good", "danger", "paper", "pane"]) {
        expect(() => resolve(token, block), `--${token} in "${theme || "standard"}"`).not.toThrow();
      }
    }
  });

  for (const theme of THEMES) {
    it(`${theme || "standard"}: ink, muted, warn, good and danger are readable on paper and pane`, () => {
      const block = theme ? blockFor(`:root[data-theme="${theme}"]`) : rootBlock;
      for (const role of ["ink", "muted", "warn", "good", "danger"]) {
        for (const bg of ["paper", "pane"]) {
          const ratio = contrast(resolve(role, block), resolve(bg, block));
          expect(ratio, `--${role} on --${bg} (${theme || "standard"})`).toBeGreaterThanOrEqual(4.5);
        }
      }
    });

    it(`${theme || "standard"}: the one filled control's ink clears 4.5:1 on its own fill`, () => {
      const block = theme ? blockFor(`:root[data-theme="${theme}"]`) : rootBlock;
      const ratio = contrast(resolve("accent-ink", block), resolve("accent", block));
      expect(ratio, `--accent-ink on --accent (${theme || "standard"})`).toBeGreaterThanOrEqual(4.5);
    });
  }
});
