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

/**
 * The same collision, one size down: no crash, just the wrong rows.
 *
 * `["orgs"]` colliding with itself was a shape mismatch — plain versus
 * infinite — and it threw. A plainer version of the same mistake does not:
 * two plain `useQuery` calls sharing one literal key but asking for different
 * PARAMETERS. react-query does not keep two answers under one key; whichever
 * call's `queryFn` last populated the cache is what every reader of that key
 * sees, so a screen that asked for a hundred rows can silently be handed
 * whichever other screen's fifty last happened to fetch.
 *
 * Found this way: `["tests"]` was read with `limit: 100` in four screens and
 * `limit: 50` in two, all six sharing one array. Every one of them was reading
 * `.data?.items`, so nothing threw — a centre admin choosing a test to assign
 * from a picker capped at fifty would simply never see number fifty-one,
 * depending on which other screen they had open first. Fixed by folding the
 * limit into the key (`["tests", 100]`, `["tests", 50]`), which is the same
 * move `PAGED` makes above: put what varies IN the key, so two readers who
 * disagree can no longer be talking about the same cache entry.
 *
 * This is a static check on the SOURCE TEXT, deliberately: two call sites can
 * only collide if they spell the key identically, and that is exactly what
 * this reads.
 */
describe("a shared query key never disagrees about what it fetches", () => {
  /** Every `useQuery({...})` call in a file, brace-matched rather than
   *  regex-truncated — a naive `\{[^}]*\}` stops at the first nested `}` in the
   *  params object and reads the wrong queryKey for the block that follows. */
  function queryBlocks(source: string): { key: string; shape: string }[] {
    const found: { key: string; shape: string }[] = [];
    for (const call of source.matchAll(/\buseQuery\(\{/g)) {
      let depth = 1;
      let i = call.index! + call[0].length;
      while (depth > 0 && i < source.length) {
        if (source[i] === "{") depth++;
        else if (source[i] === "}") depth--;
        i++;
      }
      const block = source.slice(call.index! + call[0].length, i);
      const keyMatch = block.match(/queryKey:\s*(\[[^\]]*\])/);
      if (!keyMatch) continue;
      const key = keyMatch[1]!.replace(/\s+/g, " ").trim();

      // The "shape" a caller is asking for: the endpoint plus its query
      // params for an inline fetch, or the function's name for a shared
      // loader like `loadPrincipal` — two sites naming the same function
      // agree by construction, whatever it does internally.
      const apiMatch = block.match(
        /api\.(GET|POST)\(\s*"([^"]+)"\s*(?:,\s*\{([\s\S]*)\}\s*\))?/);
      let shape: string;
      if (apiMatch) {
        const paramsMatch = (apiMatch[3] ?? "").match(/query:\s*(\{[\s\S]*\})/);
        shape = `${apiMatch[2]} ${(paramsMatch?.[1] ?? "{}").replace(/\s+/g, " ").trim()}`;
      } else {
        const fnMatch = block.match(/queryFn:\s*([A-Za-z0-9_]+)\s*[,}]/);
        shape = fnMatch ? `fn:${fnMatch[1]}` : "(unrecognized queryFn)";
      }
      found.push({ key, shape });
    }
    return found;
  }

  it("has no two screens reading one key with different endpoints or params", () => {
    const byKey = new Map<string, { shape: string; where: string }[]>();
    for (const file of files) {
      for (const { key, shape } of queryBlocks(read(file))) {
        const list = byKey.get(key) ?? [];
        list.push({ shape, where: file.slice(SRC.length + 1) });
        byKey.set(key, list);
      }
    }

    const offences: string[] = [];
    for (const [key, sites] of byKey) {
      const shapes = new Set(sites.map((s) => s.shape));
      if (shapes.size > 1) {
        offences.push(`${key}: ` +
          sites.map((s) => `${s.where} reads ${s.shape}`).join("; "));
      }
    }
    expect(offences).toEqual([]);
  });

  it("still tells two DIFFERENT keys apart", () => {
    // Guards against a scanner so eager it merges everything: `["question-types"]`
    // and `["question-types", "registry"]` are different arrays reading
    // different params on purpose — `QuestionTypes.tsx` includes deprecated
    // types for the registry screen, and nothing else should. A version of
    // this check that treated a shared PREFIX as a shared key would flag that
    // pair as colliding, which is the opposite of the point.
    const byKey = new Map<string, Set<string>>();
    for (const file of files) {
      for (const { key, shape } of queryBlocks(read(file))) {
        (byKey.get(key) ?? byKey.set(key, new Set()).get(key)!).add(shape);
      }
    }
    expect(byKey.get('["question-types"]')?.size).toBe(1);
    expect(byKey.get('["question-types", "registry"]')?.size).toBe(1);
  });

  it("finds more than a handful of query blocks, so the scanner is not blind", () => {
    const total = files.reduce((n, f) => n + queryBlocks(read(f)).length, 0);
    expect(total).toBeGreaterThan(60);
  });
});
