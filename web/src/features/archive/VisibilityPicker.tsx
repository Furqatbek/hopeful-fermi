/**
 * Who can see an asset.
 *
 * Every asset carries `visibility` and five of the six could never change it:
 * the column has a three-value CHECK on all five tables, every listing ORs four
 * routes over it, and only `PATCH /tests/{xid}` ever wrote it. An author could
 * share a whole paper with the platform and could not share the passage inside
 * it. Found by `scripts/check_write_paths.py` — `platform_global` was a value
 * the queries asked about and nothing could produce.
 *
 * **The labels say who, not what.** `org_private` renders as "Your centre",
 * because this control decides who reads a centre's material and "org_private"
 * is a column name, not an answer to that question. `platform_global` reads
 * "Every centre" and is the only one that leaves the building — which is why it
 * carries a warning line rather than a tooltip.
 *
 * Rendered read-only for anyone without `share` rather than hidden: a teacher
 * needs to know an item is platform-wide before building a paper around it, and
 * a control that vanishes teaches nobody anything. The 403 still arrives if the
 * request is made anyway, and is shown inline.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal, type Principal } from "../../api/principal";

type Endpoint =
  | "/passages/{xid}/visibility"
  | "/questions/{xid}/visibility"
  | "/question-groups/{xid}/visibility"
  | "/audio-tracks/{xid}/visibility"
  | "/cue-card-sets/{xid}/visibility";

type Visibility = "author_private" | "org_private" | "platform_global";

const LABELS: Record<Visibility, string> = {
  author_private: "You only",
  org_private: "Your centre",
  platform_global: "Every centre",
};

/** Whoever holds `share` anywhere, plus platform admin. `share` is centre admin
 *  and above, which is exactly the role set this reads.
 *
 *  An ACTIVE membership only. A `left` row still carries its old role, and a
 *  departed admin offering a control the server will refuse is a worse answer
 *  than no control. */
export function canShare(principal: Principal | null): boolean {
  if (!principal) return false;
  if (isPlatformAdmin(principal)) return true;
  return principal.memberships.some(
    (m) => m.role === "centre_admin" && m.status === "active");
}

export function VisibilityPicker({ endpoint, xid, visibility, invalidate }: {
  endpoint: Endpoint;
  xid: string;
  visibility: string | undefined;
  invalidate: unknown[];
}) {
  const queries = useQueryClient();
  const principal = useQuery({ queryKey: ["principal"], queryFn: loadPrincipal });
  const [error, setError] = useState<string | null>(null);

  const current = (visibility ?? "org_private") as Visibility;

  const share = useMutation({
    mutationFn: async (next: Visibility) => {
      const { error: failure } = await api.PUT(endpoint, {
        params: { path: { xid } },
        body: { visibility: next },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      void queries.invalidateQueries({ queryKey: invalidate });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  if (!canShare(principal.data ?? null)) {
    return <span className="muted">{LABELS[current] ?? current}</span>;
  }

  return (
    <>
      <select value={current} disabled={share.isPending}
              onChange={(event) => share.mutate(event.target.value as Visibility)}>
        {(Object.keys(LABELS) as Visibility[]).map((value) => (
          <option key={value} value={value}>{LABELS[value]}</option>
        ))}
      </select>
      {current === "platform_global" && (
        <div className="muted">
          Visible to every centre on the platform, including your competitors.
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </>
  );
}
