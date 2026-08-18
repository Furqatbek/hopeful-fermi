/**
 * The stylesheet's load-bearing rules, asserted as text.
 *
 * Not a rendering test — there is no DOM in this suite, by design. These are
 * the three rules whose absence is invisible in review, silent in TypeScript,
 * and immediately obvious to anyone actually looking at the console. Each was
 * found by measuring a running browser, and each would come back the moment
 * somebody tidies a selector.
 *
 * A stylesheet is the one place in this codebase where "it compiles" and "it
 * works" have nothing to do with each other, so the guard has to be the text.
 */

// Outside `src/`, for the same reason `vite.config.ts` is: `tsconfig.app.json`
// declares `types: ["vite/client"]` and includes only `src`, so a `node:fs`
// import inside `src` fails `npm run build` even though vitest runs it happily.
// `?raw` is not the way round it either — vitest stubs CSS imports, and the
// import arrives as an empty string, which makes every assertion below pass
// against nothing. Reading the file is the honest version, and this is where a
// file that legitimately runs in Node belongs.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  fileURLToPath(new URL("../src/styles.css", import.meta.url)), "utf8");

/** Strip comments, so prose ABOUT a rule is never mistaken for the rule. */
const rules = css.replace(/\/\*[\s\S]*?\*\//g, "");

describe("form controls", () => {
  it("does not stretch a checkbox like a text field", () => {
    // `input { flex: 1 }` inside `.choice { display: flex }` grew each checkbox
    // to fill its row — measured at 465px, 731px and 26px across one fieldset,
    // each stretched by the length of the label beside it, with the 13px tick
    // floating in the middle of that box and landing under a neighbour's words.
    // Twenty-four controls across sixteen screens.
    const bare = rules.match(/(^|[}\s])input\s*\{[^}]*\}/g) ?? [];
    for (const rule of bare) expect(rule).not.toMatch(/flex\s*:\s*1/);
  });

  it("gives the stretch only to fields that take typing", () => {
    expect(rules).toMatch(
      /input:not\(\[type="checkbox"\]\):not\(\[type="radio"\]\)\s*\{[^}]*flex\s*:\s*1/,
    );
  });

  it("pins a checkbox to its own size", () => {
    const rule = rules.match(
      /input\[type="checkbox"\]\s*,\s*input\[type="radio"\]\s*\{[^}]*\}/,
    )?.[0];
    expect(rule).toBeDefined();
    expect(rule).toMatch(/flex\s*:\s*0\s+0\s+auto/);
  });

  it("spaces a fieldset's choices instead of running them together", () => {
    // `display: block` gave three policy toggles nothing between the rows, so
    // they read as one paragraph of run-together text rather than three
    // separate decisions.
    const rule = rules.match(/(^|[}\s])fieldset\s*\{[^}]*\}/)?.[0];
    expect(rule).toMatch(/flex-direction\s*:\s*column/);
    expect(rule).toMatch(/gap\s*:/);
  });
});

describe("native controls follow the theme", () => {
  // Custom properties restyle what the STYLESHEET draws. `color-scheme`
  // restyles what the BROWSER draws for itself: checkbox glyphs, the select
  // arrow, scrollbars, the date picker. It was never declared, so on a
  // `#101216` page those stayed in light mode — three white checkboxes as the
  // brightest thing on screen, attached to the quietest control on it.
  // `(?<!-)`, because `prefers-color-scheme: dark` CONTAINS the string
  // `color-scheme: dark`. Without that, the dark assertion matched the media
  // query's own condition and stayed green when the declaration was deleted —
  // caught by reverting the fix and finding the test did not notice.
  it("declares light for the default theme", () => {
    expect(rules).toMatch(/:root\s*\{[^}]*(?<!-)color-scheme\s*:\s*light/);
  });

  it("and dark inside the dark block", () => {
    const dark = rules.match(
      /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{[\s\S]*?\n\s*\}/,
    )?.[0];
    expect(dark).toBeDefined();
    expect(dark).toMatch(/(?<!-)color-scheme\s*:\s*dark/);
  });
});

describe("a labelled control outside a form", () => {
  it("stacks the label above its control", () => {
    // Inside a `form`, the flex column does this. A filter at the top of a
    // listing has no form around it, so the label stayed inline and ran
    // straight into the select with the whitespace between them collapsed to
    // nothing — measured at 0px on seven screens (Attendance, Student progress,
    // Results, Item analysis, Speaking, Takedowns, Question types).
    const rule = rules.match(/\.page\s*>\s*label\s*\{[^}]*\}/)?.[0];
    expect(rule).toBeDefined();
    expect(rule).toMatch(/display\s*:\s*block/);
  });
});

describe("feature stylesheets use the shared tokens", () => {
  const featureCss = [
    "features/moderation/moderation.css",
    "features/registry/registry.css",
    "features/analytics/items.css",
  ].map((p) =>
    readFileSync(fileURLToPath(new URL(`../src/${p}`, import.meta.url)), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, ""),
  );

  it("takes panel corners from --r-md, not a hardcoded radius", () => {
    // Three feature panels carried `border-radius: .375rem` while every box in
    // the shared sheet uses the token — near enough to look like a mistake
    // rather than a choice, and different enough to see. Small decorative radii
    // (bars, swatches) are left alone: a panel token on a 2px bar is wrong.
    for (const css of featureCss) {
      expect(css).not.toMatch(/border-radius:\s*\.375rem/);
    }
  });

  it("keeps the discrimination chart horizontal inside a .row", () => {
    // `styles.css` carries `.row > span { flex-direction: column }` for the
    // label-over-field stacks that fill a toolbar row. `.disc` is a span and
    // sits directly in a `.row` on the findings block, so that rule turned the
    // chart on its side: halves and zero line stacked inside a 12.8px box,
    // 2.0 × 2.4px each — unreadable, on the encoding this module calls
    // "visible from across the room" — while the SAME component in a `<td>`
    // rendered 29.4 × 12.8 and correct.
    //
    // The selector must beat `.row > span` (one class, one type), so `.disc`
    // alone is not enough.
    const [items] = featureCss.slice(-1);
    expect(items).toMatch(/\.row\s*>\s*\.disc/);
    const rule = items!.match(/\.disc\s*,\s*\.row\s*>\s*\.disc\s*\{[^}]*\}/)?.[0];
    expect(rule).toBeDefined();
    expect(rule).toMatch(/flex-direction\s*:\s*row/);
  });

  it("does not shadow a shared token with a local one", () => {
    // `.items` declared `--warn`, on the stated grounds that the shared sheet
    // has none. It has one, in both themes. So this shadowed a system token
    // with a near-identical shade for everything inside `.items`, while
    // `--warn-soft` stayed global — a pair that would come out mismatched the
    // moment anyone used them together.
    const shared = rules;
    for (const css of featureCss) {
      for (const [, name] of css.matchAll(/(--[a-z][a-z0-9-]*)\s*:/g)) {
        expect(shared).not.toMatch(new RegExp(`:root\\s*\\{[^}]*${name}\\s*:`));
      }
    }
  });
});

describe("page flow", () => {
  it("separates a bare action row from the prose around it", () => {
    // `h1`, `h2`, `p` and `table` carry their own margins; a bare
    // `<div class="row">` of actions and a bare `<button>` carry none. Measured
    // at 1px and 0px between "Edit details", the button row under it and the
    // paragraph under that — three separate decisions rendering as one block.
    // The selector list must carry `.page > .row` as a term in its OWN right.
    // `[^{]*` alone was satisfied by `.page > .row + .row`, the rule that only
    // tightens two adjacent action rows — so deleting the spacing rule left
    // this green.
    const flow = rules.match(/^[^{}]*\.page\s*>\s*\.row\s*(?:,|\{)[^}]*\}/m)?.[0];
    expect(flow).toBeDefined();
    expect(flow).toMatch(/margin/);
  });
});

describe("actions in a table row look like each other", () => {
  it("keeps the row's SUBJECT in ink", () => {
    // Forty accent links down the most-read column is noise; the title earns
    // the accent on hover.
    expect(rules).toMatch(/td\s+a\s*\{[^}]*color\s*:\s*var\(--ink\)/);
  });

  it("styles .link the same on an anchor as on a button", () => {
    // `.link` was implemented for `<button>` only, and the console uses it on
    // `<a>` too — six call sites. On an anchor the class did nothing, so the
    // same class name produced two different controls:
    //   Preview  "Back to composition"  a.link  16px/400
    //            "Start a preview"      button  14.4px/550
    // The SECONDARY action came out bigger and lighter than the primary beside
    // it. In a table row: "View" 14.4px against "Archive"/"Export" at 13.6px.
    const rule = rules.match(/button\.link\s*,\s*a\.link\s*\{[^}]*\}/)?.[0];
    expect(rule).toBeDefined();
    expect(rule).toMatch(/font-size/);
    expect(rule).toMatch(/font-weight/);
  });

  it("but paints an action like its neighbours", () => {
    // "View" came out ink beside "Archive" and "Export" in accent — same job,
    // one cell apart — because those are `button.link` and it was a bare
    // `Link`. Whether an action navigates or mutates is an implementation
    // detail of the handler, not something a centre admin should be able to
    // see.
    //
    // The accent comes from the shared `.link` rule, which outranks `td a`:
    // one class plus one type beats two types. Asserted there rather than on a
    // `td a.link` override, because that override existed, fixed only the
    // colour, and left "View" a different SIZE from its neighbours — green on
    // a test that was checking the wrong half.
    const rule = rules.match(/button\.link\s*,\s*a\.link\s*\{[^}]*\}/)?.[0];
    expect(rule).toMatch(/color\s*:\s*var\(--accent\)/);
  });

  it("sizes a bare .link to its words, not to the form", () => {
    // A form is a flex COLUMN, so it stretches its children across the cross
    // axis. A `.link` inside a `.row` was fine; a bare one — "Enter the JSON
    // myself" under the answer key, and every other escape hatch — came out
    // 990px wide with its label centred in the middle of the page. Measured in
    // a browser, not spotted by reading: the rule that did it says nothing
    // about width.
    const rule = rules.match(/button\.link\s*,\s*a\.link\s*\{[^}]*\}/)?.[0];
    expect(rule).toMatch(/width\s*:\s*fit-content/);
    expect(rule).toMatch(/text-align\s*:\s*left/);
  });
});

describe("the debt the design doc listed", () => {
  it("gives every class the TSX uses a rule", () => {
    // `.panel`, `.paper`, `.side__label`, `.q-body` and `.blank` were written in
    // components with no rule anywhere. The two that mattered are in the
    // PREVIEW: `.q-body` and `.blank` are a question's prose with its answer
    // boxes inline in the sentence, which is the whole layout decision of the
    // student runner — so a teacher previewing their own paper saw something no
    // student would ever be shown.
    for (const name of ["panel", "paper", "side__label", "q-body", "blank"]) {
      expect(rules).toMatch(new RegExp(`\\.${name}\\s*[,{]`));
    }
  });

  it("declares no colour or radius token it never uses", () => {
    // The system claimed a success colour, a warning colour and a large radius,
    // and referenced none of them — while painting success in the ACCENT, which
    // is the colour of every link on the page.
    for (const token of ["--good", "--good-soft", "--warn", "--warn-soft", "--r-xl"]) {
      expect(rules).toMatch(new RegExp(`var\\(${token}\\)`));
    }
  });

  it("declares a modifier AFTER the rule it modifies", () => {
    // `.choice--grave` and `.choice` are both one class — same specificity — so
    // whichever is written second wins. The modifier was written 250 lines
    // ABOVE its base, which meant every property the two share was decided by
    // the base. Nothing looked wrong, because the only shared property happened
    // to carry the same value; the next person to add a background to `.choice`
    // would have silently turned this rule off.
    expect(rules.indexOf(".choice--grave")).toBeGreaterThan(rules.indexOf(".choice {"));
  });

  it("does not declare .small twice with two meanings", () => {
    // One global rule for small TEXT here; the small CHART that also called
    // itself `.small`, globally, in a component stylesheet, is `.chartlet` now.
    // Whichever loaded second used to win, and the order came from the module
    // graph.
    const globals = [...rules.matchAll(/^\.small\s*\{/gm)];
    expect(globals.length).toBeLessThanOrEqual(1);
  });

  it("gives a wide table its own scroll box", () => {
    // A table wider than its column pushed the whole BODY sideways, which moves
    // the header and the sidebar with it — so the thing you were trying to read
    // left the screen along with everything else.
    //
    // It cannot be done on the table: `overflow-x` needs `display: block`, and
    // a table that is a block is not a table any more — the cells stop sharing
    // column widths. Hence a wrapper, on all 44 of them.
    const rule = rules.match(/\.scroll\s*\{[^}]*\}/)?.[0];
    expect(rule).toMatch(/overflow-x\s*:\s*auto/);
    expect(rule).toMatch(/max-width\s*:\s*100%/);
  });
});
