/**
 * Retiring an asset, and putting it back.
 *
 * `archived_at` was read by four listings and written by nothing, so an author
 * told an item was burned had nothing to do about it. That gap got sharper when
 * content grants started working: revoking a grant removes one partner's
 * access, and retiring the item removes it from everybody's.
 *
 * **Archived is not deleted, and the copy has to say so**, because "Delete" on
 * a question that forty students have already answered reads as destroying
 * their marks. It is not: attempts keep resolving against archived material,
 * the item just stops appearing in the lists authors pick from. Calling the
 * button "Retire" rather than "Delete" is most of that message.
 *
 * The confirm step is deliberate and deliberately small. This is reversible —
 * `DELETE .../archive` puts it back — so a modal would be heavier than the
 * action warrants, and a two-click inline confirm is enough to stop a misclick
 * in a long table.
 *
 * **The confirmation has to leave the row, because the row leaves.** Every
 * listing that carries this control filters archived items out, so the moment
 * the mutation succeeds the button, the row and any message put beside it are
 * gone together — and an author who looked away during the refetch sees a table
 * that is one shorter than it was, with nothing saying which one went or that
 * they are the reason. That absence is the entire case for the toast; a refusal
 * still reports inline, because a refusal leaves the row exactly where it was.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useToast } from "../../app/Toast";
import { api, problemText } from "../../api/client";

type Endpoint =
  | "/passages/{xid}/archive"
  | "/questions/{xid}/archive"
  | "/question-groups/{xid}/archive"
  | "/audio-tracks/{xid}/archive"
  | "/cue-card-sets/{xid}/archive";

export function ArchiveButton({ endpoint, xid, archived, invalidate, label, name }: {
  endpoint: Endpoint;
  xid: string;
  /** Listings filter archived rows out, so this is normally false — it is here
   *  so a screen that starts showing them can offer the way back. */
  archived?: boolean | undefined;
  invalidate: unknown[];
  label?: string | undefined;
  /** The item's title, for the message left behind after its row goes. "Retired
   *  a passage" is barely worth saying in a library of two hundred; which one is
   *  the whole content of the sentence. */
  name?: string | undefined;
}) {
  const queries = useQueryClient();
  const say = useToast();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = useMutation({
    mutationFn: async () => {
      const call = archived ? api.DELETE : api.POST;
      const { error: failure } = await call(endpoint, {
        params: { path: { xid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setConfirming(false);
      setError(null);
      const what = name?.trim() ? `“${name.trim()}”` : `that ${label ?? "item"}`;
      say(archived ? `Restored ${what}.` : `Retired ${what}. It is still in every `
        + "attempt that used it; the way back is the archived filter.");
      void queries.invalidateQueries({ queryKey: invalidate });
    },
    // Retiring is centre-admin and above, so a teacher gets a refusal here
    // rather than a control that was never going to work. Shown inline: the
    // reason matters more than the failure.
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  if (archived) {
    return (
      <button className="link" disabled={act.isPending}
              onClick={() => act.mutate()}>
        Restore
      </button>
    );
  }

  return (
    <>
      {confirming ? (
        <span className="row">
          <button className="link" disabled={act.isPending}
                  onClick={() => act.mutate()}>
            {act.isPending ? "Retiring…" : "Confirm"}
          </button>
          <button className="link" onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </span>
      ) : (
        <button
          className="link"
          title={`Retire this ${label ?? "item"}. Attempts that already use it `
                 + "keep working; it stops appearing in the lists authors pick from."}
          onClick={() => { setError(null); setConfirming(true); }}
        >
          Retire
        </button>
      )}
      {error && <span className="error"> {error}</span>}
    </>
  );
}
