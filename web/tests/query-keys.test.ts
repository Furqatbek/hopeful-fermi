/**
 * One cache, two shapes, and the screen that went blank.
 *
 * react-query keeps plain and infinite queries in the SAME cache, keyed the
 * same way, and stores different things under them: a plain query caches the
 * response body, an infinite one caches `{pages, pageParams}`. Nothing in
 * TypeScript relates the two — the key is `unknown[]` on both sides — so a key
 * used both ways compiles, and whichever query mounts first decides what is in
 * the entry.
 *
 * The infinite side is the one that dies. `getNextPageParam` reads
 * `pages.length` off a `pages` a plain response does not have, and it throws
 * during render, which React turns into an unmounted tree: not a broken table,
 * a blank page.
 *
 * Three keys were used both ways when paging landed, and the worst was `["orgs"]`
 * — nine screens read it plainly, the platform listing paged it. **Measured in a
 * browser: `/centre` rendered nothing at all for a centre admin**, every time,
 * because the roster's own org lookup populates `["orgs"]` before the members
 * listing mounts.
 *
 * `usePaged` appends `PAGED` to every key it is given, which makes the
 * collision unrepresentable. This is the test that says so, plus the one that
 * catches somebody reaching past it.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { PAGED } from "../src/app/paging";

const SRC = fileURLToPath(new URL("../src", import.meta.url));

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(path) && !path.endsWith(".test.ts") ? [path] : [];
  });
}

const files = sources(SRC);
const read = (f: string) => readFileSync(f, "utf8");

describe("paged and plain listings never share a cache entry", () => {
  it("keeps the separator in `usePaged` rather than at the call sites", () => {
    // A rule five files have to remember is a rule the sixth breaks. The suffix
    // is applied inside the hook, so a call site cannot collide by forgetting.
    const paging = read(join(SRC, "app/paging.tsx"));
    expect(paging).toMatch(/queryKey:\s*\[\s*\.\.\.queryKey,\s*PAGED\s*\]/);
  });

  it("uses a SUFFIX, so existing invalidations still reach the listing", () => {
    // react-query matches `invalidateQueries` by key PREFIX. A suffix keeps
    // `["orgs"]` refreshing `["orgs", PAGED]`, which is why no screen that
    // invalidates a listing had to be touched — and a prefix would have
    // silently stopped every one of them from refreshing anything.
    expect(PAGED).toBeTruthy();
    const paging = read(join(SRC, "app/paging.tsx"));
    expect(paging).not.toMatch(/queryKey:\s*\[\s*PAGED,/);
  });

  it("has no screen writing the marker by hand", () => {
    // The point of the marker is that only the hook sets it. A screen that
    // spells it into a plain `useQuery` has recreated the collision with extra
    // steps.
    const offenders = files
      .filter((f) => !f.endsWith("app/paging.tsx"))
      .filter((f) => read(f).includes(PAGED));
    expect(offenders).toEqual([]);
  });

  it("has no screen reaching for `useInfiniteQuery` directly", () => {
    // The hole the fix leaves, and the only one. Three keys collided at the
    // SOURCE level and still do — `["orgs"]` is written identically in
    // `Organizations` and in eight plain readers — because the suffix is added
    // inside the hook, which is exactly what makes the mistake unrepresentable
    // for anyone going through it. Somebody calling `useInfiniteQuery` on their
    // own is going around it, and lands straight back on a blank `/centre`.
    //
    // So the guard is not "these keys differ" — after the fix they do not need
    // to, and a test asserting they did would have to be deleted the first time
    // a listing was paged under an existing key. It is "there is one door".
    const offenders = files
      .filter((f) => !f.endsWith("app/paging.tsx"))
      .filter((f) => /\buseInfiniteQuery\b/.test(read(f)))
      .map((f) => f.slice(SRC.length + 1));
    expect(offenders).toEqual([]);
  });

  it("finds this console's listings at all", () => {
    // Guards the guards above: all four assert the ABSENCE of something, and
    // absence is also what an empty file list produces.
    const paged = files.filter((f) => /\busePaged\(/.test(read(f)));
    expect(paged.length).toBeGreaterThan(3);
    expect(files.length).toBeGreaterThan(50);
  });
});
