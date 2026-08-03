/**
 * Editing a versioned draft, with the optimistic lock the contract requires.
 *
 * All five draft-edit endpoints declare `If-Match` **required**, and the tag has
 * to come from a fresh read of the thing being edited — a listing does not carry
 * one, and a tag held from an earlier read is exactly what the lock exists to
 * reject. So this reads before it writes, every time.
 *
 * That extra GET is not waste. Two teachers editing one draft is the case the
 * lock is for, and the round trip is what makes the second one lose loudly
 * rather than silently win. A 412 here means "somebody changed this while you
 * were typing" and the only correct response is to reload — never to retry with
 * the same tag, which would just fail again, and never to strip the header,
 * which would restore the bug.
 *
 * Published versions are refused by the server (`version_immutable`). Callers
 * should not offer the control at all in that case; this reports the refusal
 * rather than pretending it cannot happen.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, problemText } from "../../api/client";

type Endpoint =
  | "/question-versions/{xid}"
  | "/passage-versions/{xid}"
  | "/question-group-versions/{xid}";

export function useVersionEdit(endpoint: Endpoint, invalidate: unknown[]) {
  const queries = useQueryClient();

  return useMutation({
    mutationFn: async ({ xid, body }: { xid: string; body: Record<string, unknown> }) => {
      // Read for the tag. Not cached: a tag is only worth having if it describes
      // the state this write is actually racing against.
      const read = await api.GET(endpoint, { params: { path: { xid } } });
      const tag = read.response.headers.get("ETag");
      if (!tag) {
        throw new Error("Could not read this version. Reload and try again.");
      }
      const { error: failure } = await api.PATCH(endpoint, {
        params: { path: { xid }, header: { "If-Match": tag } },
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        body: body as any,
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      void queries.invalidateQueries({ queryKey: invalidate });
    },
  });
}

/** A 412 needs different words from every other refusal: nothing is wrong with
 *  the edit, it was just computed against a version that has moved on. */
export function editError(error: unknown): string {
  const text = problemText(error) || String(error);
  return text.includes("stale_version") || text.includes("changed since")
    ? "Somebody else changed this while you were editing. Reload to see their "
      + "version, then make your change again."
    : text;
}
