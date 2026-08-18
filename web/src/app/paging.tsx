/**
 * Reading a listing that is longer than one page.
 *
 * The contract has declared `{items, next_cursor}` on every listing since it was
 * written, and until now the console consumed the cursor **nowhere**: the only
 * mentions of `next_cursor` in this codebase were comments explaining that it
 * was always null. Screens coped by asking for a big `limit` — the roster asked
 * for 200 — which is a workaround that silently truncates at whatever number
 * somebody guessed, on the screen that lists a centre's people.
 *
 * **"Show more", not numbered pages.** A cursor is a position, not an offset, so
 * there is no page 7 to jump to — and that is the right trade rather than a
 * limitation: an offset shifts under you when a row is added or removed while
 * you read, which on a roster being edited means a student who never appears.
 * Appending also matches what the screens are for. A teacher scanning for one
 * name wants a longer list; nobody wants to remember which page it was on.
 */

import { type InfiniteData, useInfiniteQuery } from "@tanstack/react-query";

/** What every paged listing in this contract returns. */
export type Page<T> = { items?: T[]; next_cursor?: string | null };

/**
 * Appended to every key below, and the reason is a crash this shipped with.
 *
 * An infinite query caches `{pages, pageParams}` where a plain one caches the
 * response body, and react-query keys them in ONE cache. Three keys were used
 * both ways — `["orgs"]` by nine plain readers and by the platform listing,
 * `["members", orgXid]` by `Seats` and `ClassMembers` and by the roster,
 * `["audio-tracks"]` by `AddSection` and the library — so whichever query
 * populated the entry first decided its shape, and the other one read it.
 *
 * The infinite side is the one that dies: `getNextPageParam` does
 * `pages.length` on a `pages` that a plain response does not have, which throws
 * during render and takes the whole screen with it. **Measured: `/centre` was a
 * blank page** for any centre admin, every time, because the sidebar's org
 * lookup populates `["orgs"]` plainly before the roster ever mounts.
 *
 * Fixed here rather than at the five call sites, because a rule five files have
 * to remember is a rule that gets broken by the sixth. A SUFFIX rather than a
 * prefix so the existing invalidations keep working: react-query matches
 * `invalidateQueries` by key prefix, so `["orgs"]` still refreshes
 * `["orgs", "__paged"]`, and no screen that invalidates a listing had to change.
 */
export const PAGED = "__paged";

/**
 * One listing, fetched a page at a time.
 *
 * `initialPageParam` is null and the page function takes `cursor: string | null`
 * — the FIRST request must not send a cursor at all, and an empty string is not
 * the same thing to a server that decodes what it is given.
 */
export function usePaged<T>(
  queryKey: unknown[],
  fetchPage: (cursor: string | null) => Promise<Page<T>>,
  options: {
    enabled?: boolean;
    /** Poll while this says so — the audio library refetches while anything is
     *  still transcoding. Given the rows LOADED SO FAR rather than one page,
     *  because "is anything still processing" is a question about the list. */
    refetchInterval?: (items: T[]) => number | false;
  } = {},
) {
  const query = useInfiniteQuery({
    queryKey: [...queryKey, PAGED],
    queryFn: ({ pageParam }) => fetchPage(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last: Page<T>) => last.next_cursor ?? null,
    enabled: options.enabled ?? true,
    ...(options.refetchInterval
      ? {
          refetchInterval: (q: {
            state: { data?: InfiniteData<Page<T>, string | null> | undefined };
          }) =>
            options.refetchInterval!(
              (q.state.data?.pages ?? []).flatMap((p) => p.items ?? [])),
        }
      : {}),
  });

  return {
    items: (query.data?.pages ?? []).flatMap((p) => p.items ?? []),
    hasMore: query.hasNextPage,
    more: () => { void query.fetchNextPage(); },
    loadingMore: query.isFetchingNextPage,
    isPending: query.isPending,
    isError: query.isError,
    error: query.error,
    refetch: query.refetch,
  };
}

/**
 * The control under a listing.
 *
 * **Absent when there is nothing more**, like every other control in this
 * console — a greyed-out "Show more" is an invitation to keep clicking a
 * question that has already been answered. The count is the point of the line
 * when the list ends: "all 34" tells a centre admin they have seen everybody,
 * which is the thing the old silent truncation could never say.
 */
export function Pager({ shown, hasMore, onMore, loading, noun }: {
  shown: number;
  hasMore: boolean;
  onMore: () => void;
  loading?: boolean;
  /** Plural, lower case: "students", "passages". */
  noun: string;
}) {
  if (shown === 0) return null;
  return (
    <p className="muted pager">
      {hasMore ? `${shown} ${noun} so far. ` : `All ${shown} ${noun}. `}
      {hasMore && (
        <button type="button" className="link" onClick={onMore} disabled={loading}>
          {loading ? "Loading…" : "Show more"}
        </button>
      )}
    </p>
  );
}
