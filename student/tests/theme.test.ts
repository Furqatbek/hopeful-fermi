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
