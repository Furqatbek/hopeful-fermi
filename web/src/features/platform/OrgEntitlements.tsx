/**
 * What a centre holds, and taking it back.
 *
 * `entitlements.revoked_at` was read by every access decision in the product and
 * written by nothing: `entitlements` is the one table the whole product asks "is
 * this allowed" against, both readers filter `revoked_at IS NULL`, and the
 * column could never be anything else. A payment reversed at the bank left the
 * feature switched on for ever.
 *
 * **The listing is half the fix and the more easily forgotten half.**
 * `GET /me/entitlements` is scoped to the actor, so before this panel a platform
 * admin could not see what any centre had bought and had no way to reach the id
 * the revoke takes. An action with no way to find its subject is the shape
 * `check_console_coverage.py` exists to catch, and shipping the revoke alone
 * would have been a fresh instance of it.
 *
 * Revoked rows stay on the list, greyed, with their reason. This is the screen a
 * billing dispute is argued from — "we were charged after we cancelled" is
 * answered by the dates and the note, and a row that vanishes answers it with
 * nothing.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";

export function OrgEntitlements({ orgXid, name }: { orgXid: string; name: string }) {
  const queries = useQueryClient();
  const [revoking, setRevoking] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const held = useQuery({
    queryKey: ["admin-entitlements", orgXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET(
        "/admin/orgs/{xid}/entitlements", { params: { path: { xid: orgXid } } });
      if (failure) throw failure;
      return data;
    },
  });

  const revoke = useMutation({
    mutationFn: async (xid: string) => {
      const { error: failure } = await api.POST("/admin/entitlements/{xid}/revoke", {
        params: { path: { xid } },
        body: { reason },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setRevoking(null);
      setReason("");
      setError(null);
      void queries.invalidateQueries({ queryKey: ["admin-entitlements", orgXid] });
      // The centre's own view of what it holds is now wrong.
      void queries.invalidateQueries({ queryKey: ["entitlements"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  return (
    <div className="panel">
      <h3>{name} — entitlements</h3>
      {held.isError && <p className="error">{problemText(held.error)}</p>}
      {held.data?.length === 0 && (
        <p className="muted">This centre holds nothing. Nothing has been bought
          for it, and no seat licence has been granted.</p>
      )}
      {held.data && held.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>What</th><th>How</th><th>Left</th><th>Runs to</th><th>State</th><th />
            </tr>
          </thead>
          <tbody>
            {held.data.map((row) => (
              <tr key={row.xid} className={row.revoked_at ? "muted" : undefined}>
                <td>{row.feature}</td>
                <td className="muted">{row.source_kind}</td>
                <td className="num">
                  {row.quantity === null || row.quantity === undefined
                    ? "unlimited"
                    : `${row.remaining ?? 0} of ${row.quantity}`}
                </td>
                <td className="muted">
                  {row.expires_at
                    ? new Date(row.expires_at).toLocaleDateString()
                    : "no end date"}
                </td>
                <td className="muted">
                  {row.revoked_at
                    ? `revoked ${new Date(row.revoked_at).toLocaleDateString()}` +
                      (row.revoked_reason ? ` — ${row.revoked_reason}` : "")
                    : "live"}
                </td>
                <td>
                  {row.revoked_at ? null : revoking === row.xid ? (
                    <span className="row">
                      <input value={reason} autoFocus
                             placeholder="Why — a chargeback, a refund, a mistake"
                             onChange={(event) => setReason(event.target.value)} />
                      {/* Required by the endpoint and required here, rather
                          than letting the 422 teach it. This note is what a
                          centre is shown when it asks why access stopped. */}
                      <button className="link"
                              disabled={revoke.isPending || reason.trim() === ""}
                              onClick={() => revoke.mutate(row.xid)}>
                        {revoke.isPending ? "Revoking…" : "Confirm"}
                      </button>
                      <button className="link" onClick={() => {
                        setRevoking(null);
                        setReason("");
                      }}>Cancel</button>
                    </span>
                  ) : (
                    <button className="link" onClick={() => setRevoking(row.xid)}>
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error && <p className="error">{error}</p>}
      <p className="muted">
        Revoking takes effect on the next request — access is resolved per call,
        not cached in a token. Revoked rows stay listed with their reason,
        because this is what a billing dispute is settled against.
      </p>
    </div>
  );
}
