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

import { ALL, PAGED } from "../src/app/paging";

const SRC = fileURLToPath(new URL("../src", import.meta.url));
const CONTRACT = fileURLToPath(new URL("../../openapi/openapi.yaml", import.meta.url));

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
    // steps. `ALL` is the same marker for the walk-to-the-end hook, and the
    // same rule.
    const offenders = files
      .filter((f) => !f.endsWith("app/paging.tsx"))
      .filter((f) => read(f).includes(PAGED) || read(f).includes(ALL));
    expect(offenders).toEqual([]);
  });

  it("walks a cursor only inside the two hooks", () => {
    // `usePaged` follows `next_cursor` a page at a time; `useAll` follows it
    // to the end for a picker. A screen reading `.next_cursor` itself is a
    // third way of paging, keyed however its author remembered to — which on
    // `/centre` is one plain query away from the blank page above.
    const paging = read(join(SRC, "app/paging.tsx"));
    expect(paging).toMatch(/\.next_cursor\b/);
    expect(paging).toMatch(/export function useAll</);
    const offenders = files
      .filter((f) => !f.endsWith("app/paging.tsx"))
      .filter((f) => /\.next_cursor\b/.test(read(f)))
      .map((f) => f.slice(SRC.length + 1));
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

/**
 * An invalidation that matches nothing is a no-op with a success toast.
 *
 * `invalidateQueries` matches by key PREFIX, and a prefix of nothing is
 * nothing. The group library passed `invalidate={["groups"]}` to its retire
 * and visibility controls while its listing was keyed `["question-groups"]`:
 * "Retired …" appeared and the row stayed until a remount, and the visibility
 * select snapped back to the cached value the moment it was changed. No type
 * relates the two arrays, so the compiler had nothing to say.
 *
 * Every other library passed its real key, which is how it was found — and
 * why this is a scan of every file rather than a fix to one: the next library
 * is a copy of an existing one, and the copied literal is the first thing
 * edited and the last thing checked.
 *
 * Checked against keys anywhere in web/src, not the same file: invalidations
 * legitimately cross files (`AttachGroup` reads the key `GroupLibrary`
 * refreshes), and `usePaged`/`useAll` calls carry their key as a first
 * argument rather than under `queryKey:`.
 */
describe("an invalidation names a key some query actually reads", () => {
  /** `["a", b]` → `['"a"', 'b']`, whitespace-normalised. */
  const elements = (literal: string) =>
    literal.slice(1, -1).split(",").map((e) => e.replace(/\s+/g, " ").trim())
      .filter((e) => e !== "");

  function knownKeys(): string[][] {
    const keys: string[][] = [];
    for (const file of files) {
      const source = read(file);
      for (const m of source.matchAll(
        /(?:queryKey:|\busePaged\(|\buseAll\()\s*(\[[^\]]*\])/g)) {
        keys.push(elements(m[1]!));
      }
    }
    return keys;
  }

  const isPrefixOf = (short: string[], long: string[]) =>
    short.length <= long.length && short.every((e, i) => e === long[i]);

  it("has no `invalidate={[...]}` that is a prefix of no known key", () => {
    const keys = knownKeys();
    const offences: string[] = [];
    for (const file of files) {
      for (const m of read(file).matchAll(/invalidate=\{(\[[^\]]*\])\}/g)) {
        const wanted = elements(m[1]!);
        if (!keys.some((key) => isPrefixOf(wanted, key))) {
          offences.push(`${file.slice(SRC.length + 1)}: ${m[1]!.replace(/\s+/g, " ")}`);
        }
      }
    }
    expect(offences).toEqual([]);
  });

  it("finds the invalidations at all", () => {
    // Five libraries pass two each. A regex that matched none would pass the
    // check above by finding nothing to fail.
    const total = files.reduce(
      (n, f) => n + [...read(f).matchAll(/invalidate=\{(\[[^\]]*\])\}/g)].length, 0);
    expect(total).toBeGreaterThan(8);
    expect(knownKeys().length).toBeGreaterThan(60);
  });
});

/**
 * A `limit` above what the contract allows is a truncation the server has
 * not yet been asked to refuse.
 *
 * The shared `Limit` parameter declares `maximum: 100`, and four pickers sent
 * 200 — accepted today only because the handlers bind `limit: int = 25` with
 * no bound, so the console was one `le=100` away from four 422s. The two
 * member pickers now walk the cursor (`useAll`); the two question pickers ask
 * for one page of the maximum, because `GET /questions` has an anti-scrape
 * budget a walk would spend.
 *
 * Read from the contract rather than hard-coded at 100: `/content-grants` and
 * `/admin/takedowns` declare their own `maximum: 200` inline, and a scanner
 * that flagged them would be re-litigating a limit the contract grants.
 */
describe("no listing asks for more than its contract allows", () => {
  const yaml = readFileSync(CONTRACT, "utf8");

  /** The declared maximum for `limit` on one path, or null when the path
   *  declares no `limit` at all. */
  function contractMax(path: string): number | null {
    const escaped = path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const start = yaml.search(new RegExp(`^  ${escaped}:\\s*$`, "m"));
    if (start < 0) return null;
    const rest = yaml.slice(start + 1);
    const end = rest.search(/^  \//m);
    const block = end < 0 ? rest : rest.slice(0, end);
    const inline = block.match(/name: limit,[^}\n]*maximum: (\d+)/);
    if (inline) return Number(inline[1]);
    if (/#\/components\/parameters\/Limit/.test(block)) {
      const shared = yaml.match(/^    Limit:\n\s+name: limit\n[\s\S]*?maximum: (\d+)/m);
      return shared ? Number(shared[1]) : null;
    }
    return null;
  }

  /** Every `api.GET("<path>", {...})` call in a file, paren-matched, with the
   *  `limit:` literal it sends if any. */
  function listingCalls(source: string): { path: string; limit: number | null }[] {
    const calls: { path: string; limit: number | null }[] = [];
    for (const m of source.matchAll(/\bapi\.GET\(\s*"([^"]+)"/g)) {
      let depth = 1;
      let i = m.index! + "api.GET(".length;
      while (depth > 0 && i < source.length) {
        if (source[i] === "(") depth++;
        else if (source[i] === ")") depth--;
        i++;
      }
      const args = source.slice(m.index!, i);
      const limit = args.match(/\blimit:\s*(\d+)/);
      calls.push({ path: m[1]!, limit: limit ? Number(limit[1]) : null });
    }
    return calls;
  }

  it("reads the contract's shared maximum", () => {
    expect(contractMax("/orgs/{xid}/members")).toBe(100);
    expect(contractMax("/questions")).toBe(100);
    // Declared inline, and larger: the reason this is not a bare `> 100`.
    expect(contractMax("/admin/takedowns")).toBe(200);
  });

  it("sends no `limit` above the declared maximum", () => {
    const offences: string[] = [];
    for (const file of files) {
      for (const { path, limit } of listingCalls(read(file))) {
        if (limit === null) continue;
        const max = contractMax(path);
        if (max === null) {
          offences.push(`${file.slice(SRC.length + 1)}: ${path} declares no limit, sent ${limit}`);
        } else if (limit > max) {
          offences.push(`${file.slice(SRC.length + 1)}: ${path} allows ${max}, sent ${limit}`);
        }
      }
    }
    expect(offences).toEqual([]);
  });

  it("finds the console's listing calls at all", () => {
    const withLimit = files.reduce(
      (n, f) => n + listingCalls(read(f)).filter((c) => c.limit !== null).length, 0);
    expect(withLimit).toBeGreaterThan(15);
  });
});
