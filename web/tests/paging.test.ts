/**
 * The walk to the end, proven by walking.
 *
 * `useAll` exists so a picker can offer the (N+1)th student: `limit: 200` on
 * the two member pickers silently stopped at whatever one page held, and the
 * fix was to follow `next_cursor` until it is null. Nothing else in this
 * console's tests could tell a walk from a single page — the scanners in
 * `query-keys.test.ts` read source text, and a `useAll` that fetched its first
 * page and stopped satisfied every one of them. There is no DOM here to mount
 * the hook in, so the loop lives in `walkAll`, a plain function these tests
 * can call with a fake page fetcher.
 */

import { describe, expect, it } from "vitest";

import { MAX_PAGES, type Page, walkAll } from "../src/app/paging";

/** A listing served in fixed pages, remembering which cursors it was asked for. */
function listing<T>(pages: Page<T>[]) {
  const asked: (string | null)[] = [];
  const fetchPage = async (cursor: string | null): Promise<Page<T>> => {
    asked.push(cursor);
    const index = cursor === null ? 0 : Number(cursor);
    return pages[index] ?? {};
  };
  return { asked, fetchPage };
}

describe("walkAll", () => {
  it("follows next_cursor from the first page to the last, in order", async () => {
    // The first request carries no cursor — an empty string is not the same
    // thing to a server that decodes what it is given — and each request
    // after carries exactly the cursor the previous page returned.
    const { asked, fetchPage } = listing<string>([
      { items: ["a", "b"], next_cursor: "1" },
      { items: ["c"], next_cursor: "2" },
      { items: ["d", "e"], next_cursor: null },
    ]);
    await expect(walkAll(fetchPage)).resolves.toEqual(["a", "b", "c", "d", "e"]);
    expect(asked).toEqual([null, "1", "2"]);
  });

  it("stops at a page with no cursor at all, not only at an explicit null", async () => {
    // The contract marks `next_cursor` optional, and a handler that omits it
    // on the last page means the same thing as one that sends null.
    const { asked, fetchPage } = listing<number>([
      { items: [1], next_cursor: "1" },
      { items: [2] },
    ]);
    await expect(walkAll(fetchPage)).resolves.toEqual([1, 2]);
    expect(asked).toEqual([null, "1"]);
  });

  it("gives up after MAX_PAGES when the cursor never ends", async () => {
    // A cursor that is still going after fifty pages is a server bug, and the
    // right answer to one is a truncated list rather than a tab that fetches
    // for ever.
    let calls = 0;
    const forever = async (cursor: string | null): Promise<Page<number>> => {
      calls++;
      return { items: [calls], next_cursor: String(Number(cursor ?? "0") + 1) };
    };
    const items = await walkAll(forever);
    expect(calls).toBe(MAX_PAGES);
    expect(items).toHaveLength(MAX_PAGES);
  });
});
