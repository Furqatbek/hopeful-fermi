/**
 * The rule that lets this console have icons at all.
 *
 * Every control here is a WORD — "Where used", "Billing", "Retire" — because a
 * word says what it does and a glyph needs a legend. Introducing `Icon.tsx` is
 * the first thing in this codebase that could erode that, and it erodes the way
 * every icon system erodes: not by decision, but by one row's action being
 * tight for space, so the label comes out and the glyph stays. After that the
 * next one has a precedent.
 *
 * So the rule is checked rather than written down. **An icon may never be the
 * only content of a button or a link.** It sits beside a word or it does not
 * ship.
 *
 * A source assertion, like `table-headers.test.ts` and for the same reason:
 * this suite has no DOM by design, and the mistake is made while typing the
 * JSX. What it cannot see — a label built from an expression that happens to be
 * empty at runtime — is not the failure mode this guards. The failure mode is
 * somebody deleting the word.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { iconForStatus } from "../src/app/Icon";

const SRC = fileURLToPath(new URL("../src", import.meta.url));

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return tsxFiles(path);
    return path.endsWith(".tsx") ? [path] : [];
  });
}

/** `<button …>…</button>` and `<a …>…</a>`, innards included, non-greedy. */
const CONTROLS = /<(button|a|Link)\b[^>]*>([\s\S]*?)<\/\1>/g;

/** Everything a reader would HEAR: JSX text, and the string literals inside
 *  expressions, which is how this console writes a conditional label
 *  (`{playing ? "Close" : "Play"}`). Comments are stripped first so prose about
 *  a control is never mistaken for the control's label. */
function speech(inner: string): string {
  const withoutComments = inner.replace(/\{\/\*[\s\S]*?\*\/\}/g, "");
  const text = withoutComments.replace(/<[^>]*>/g, " ").replace(/\{[^{}]*\}/g, " ");
  const literals = [...withoutComments.matchAll(/["'`]([^"'`]+)["'`]/g)].map((m) => m[1]!);
  return (text + " " + literals.join(" ")).replace(/\s+/g, " ").trim();
}

describe("icons are never the whole of a control", () => {
  const files = tsxFiles(SRC);

  it("finds this console's controls at all", () => {
    // Guards the guard. If the markup ever moves somewhere the regex does not
    // see, everything below would pass by matching nothing.
    const total = files.reduce(
      (n, f) => n + [...readFileSync(f, "utf8").matchAll(CONTROLS)].length, 0);
    expect(total).toBeGreaterThan(100);
  });

  it("never puts an <Icon> or <Status> in a button or link with no words", () => {
    const offences: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      for (const control of source.matchAll(CONTROLS)) {
        const inner = control[2]!;
        if (!/<(Icon|Status)\b/.test(inner)) continue;
        if (speech(inner) === "") {
          offences.push(`${file.slice(SRC.length + 1)}: <${control[1]}> is icon-only`);
        }
      }
    }
    expect(offences).toEqual([]);
  });

  it("keeps every icon out of the accessibility tree", () => {
    // The word beside it IS the accessible name. An icon that also announced
    // itself would read "ready ready", and one that announced itself WRONG —
    // `title` on an svg becomes its name — would read the shape's name over the
    // status's.
    const icon = readFileSync(join(SRC, "app/Icon.tsx"), "utf8");
    expect(icon).toMatch(/aria-hidden="true"/);
    expect(icon).not.toMatch(/<title>/);
  });
});

describe("the status vocabulary", () => {
  it("gives an unknown status no icon rather than a default one", () => {
    // A state added to the server should look unfamiliar on the screen until
    // somebody decides what it means. Quietly giving it a tick is how a screen
    // lies about a row it does not understand.
    expect(iconForStatus("teleported")).toBeNull();
    expect(iconForStatus(null)).toBeNull();
    expect(iconForStatus(undefined)).toBeNull();
    expect(iconForStatus("")).toBeNull();
  });

  it("reads a status whatever case it arrives in", () => {
    expect(iconForStatus("FAILED")).toBe("failed");
    expect(iconForStatus("Ready")).toBe("ok");
  });

  it("has a mark for every status a column can render", () => {
    // Transcribed from the CHECK constraints in `migrations/versions/`, which
    // are the only list of what these columns can hold. A word missing here is
    // a row with a blank where its neighbours have a mark, which reads as a
    // rendering fault rather than as a state — and it is found by opening the
    // screen on the day that row exists, not before.
    const VOCABULARIES: Record<string, string[]> = {
      // 0007 content_assets — the audio library's own column. NOT the same list
      // as `media_assets` below, which was the first version of this test and
      // was caught by the database refusing the seed row that would have proved
      // it: `uploading` is not a value this column can hold.
      audio_tracks: ["draft", "processing", "ready", "failed", "archived"],
      // 0006 media — the upload behind a track. Not rendered as a column today;
      // mapped because the words are the ones a transcode failure arrives as,
      // and a screen that starts showing them should not have to come back here.
      media_assets: ["uploading", "processing", "ready", "failed", "quarantined",
                     "removed"],
      // 0008 — a test's versions
      test_versions: ["draft", "in_review", "published", "archived"],
      // 0014 — the safety queue
      safety_reports: ["new", "triage", "investigating", "actioned", "dismissed"],
      // 0014 — takedowns
      takedown_requests: ["received", "reviewing", "upheld", "rejected",
                          "counter_noticed", "withdrawn"],
      // 0003 — the roster, and the organizations list
      org_memberships: ["invited", "active", "suspended", "left"],
      organizations: ["pending", "active", "suspended", "closed"],
      // 0011 — results, and an assignment's progress (whose `not_started` is
      // computed in `teaching.py` rather than stored)
      attempts: ["issued", "in_progress", "submitted", "scored", "abandoned",
                 "voided", "not_started"],
      // 0011 — the regrade jobs listing
      regrade_jobs: ["planning", "ready", "running", "completed", "failed",
                     "cancelled"],
      // 0012 — competitions
      competitions: ["scheduled", "registration", "lobby", "live", "grading",
                     "final", "cancelled"],
      // 0015 — billing, on the usage panel
      invoices: ["pending", "awaiting_payment", "paid", "cancelled", "refunded",
                 "failed", "expired"],
    };

    const missing: string[] = [];
    for (const [table, words] of Object.entries(VOCABULARIES)) {
      for (const word of words) {
        if (iconForStatus(word) === null) missing.push(`${table}.${word}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("does not paint an ending as a failure or a success", () => {
    // The axis is "does this row need someone?", not good and bad. A `rejected`
    // takedown is not a failure — somebody looked and refused it, correctly —
    // and an `archived` version is not a success. Both are simply over, and a
    // column that paints the first red and the second green lies twice.
    for (const over of ["archived", "rejected", "withdrawn", "dismissed",
                        "cancelled", "left", "closed", "refunded"]) {
      expect(iconForStatus(over)).toBe("done");
    }
  });

  it("marks only what has gone wrong as failed", () => {
    for (const bad of ["failed", "quarantined", "suspended", "expired"]) {
      expect(iconForStatus(bad)).toBe("failed");
    }
  });
});
