/**
 * Taking somebody off the centre's roster.
 *
 * `org_memberships.left_at` was read and written by nothing, and so was
 * `status` here — a centre could enrol somebody and had no way to un-enrol
 * them. A student could be taken out of a class (`ClassMembers` has had that
 * control) and stayed a member of the organization for ever: still on this
 * list, still a valid assignment target, still holding a seat.
 *
 * **The copy names the two consequences, because the endpoint has two.** It
 * ends their class memberships in the same transaction — leaving the centre and
 * leaving its classes are separate tables, and removing only the first would
 * take somebody off this list while their class kept delivering mocks to them.
 * It does NOT release their seat: a seat is paid for and has its own control on
 * the Seats panel above, and quietly handing one back as a side effect of a
 * roster edit is how a centre discovers it has been billed for something it did
 * not do.
 *
 * The confirm is inline rather than a modal, matching `ArchiveButton` — but
 * unlike retiring an asset this is NOT reversible from the console: putting
 * somebody back means a new invitation. The confirm copy says so, and that
 * sentence is the reason this is not simply a link.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";

export function RemoveMember({ orgXid, userXid, name }: {
  orgXid: string;
  userXid: string;
  name: string;
}) {
  const queries = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.DELETE("/orgs/{xid}/members/{user_xid}", {
        params: { path: { xid: orgXid, user_xid: userXid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setConfirming(false);
      setError(null);
      void queries.invalidateQueries({ queryKey: ["members", orgXid] });
      // Their class memberships ended too, so any open class panel is stale.
      void queries.invalidateQueries({ queryKey: ["cohort-members"] });
    },
    // `last_centre_admin` arrives here, and it is the one refusal an admin is
    // likely to hit: removing yourself when you are the only one left. The
    // problem detail says to promote somebody first, so show it rather than a
    // generic failure.
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  if (!orgXid || !userXid) return null;

  if (confirming) {
    return (
      <span className="row">
        <button className="link" disabled={remove.isPending}
                onClick={() => remove.mutate()}>
          {remove.isPending ? "Removing…" : "Confirm"}
        </button>
        <button className="link" onClick={() => setConfirming(false)}>
          Cancel
        </button>
        <span className="muted">
          Ends {name}&rsquo;s classes too. Their seat stays assigned. Re-adding
          them needs a new invitation.
        </span>
        {error && <span className="error">{error}</span>}
      </span>
    );
  }

  return (
    <span className="row">
      <button className="link" onClick={() => setConfirming(true)}>Remove</button>
      <CloseAccount userXid={userXid} name={name} />
      {error && <span className="error">{error}</span>}
    </span>
  );
}

/**
 * Closing the account itself. Platform admin only, and adjacent to Remove on
 * purpose: the two are easy to confuse and the copy is what separates them.
 *
 * `users.deleted_at` was read in seven places — every sign-in path, the media
 * grant check, the assignment targeter — and written by nothing, so "this
 * account is closed" was designed right through the query layer and reachable
 * from nowhere. A student who asked to be removed could only be suspended, which
 * is a moderation verdict on their conduct and the wrong record to leave against
 * somebody who simply left.
 *
 * **Closure is not erasure and the confirm says so.** It stops sign-in and ends
 * every membership; it does not delete attempts, recordings or audit rows, which
 * are evidence in a copyright or safety investigation and a centre's exam
 * records besides. An admin who reads "Close account" as "erase this person"
 * and tells a family so has been misled by this screen, so the sentence is here
 * rather than in a runbook.
 */
function CloseAccount({ userXid, name }: { userXid: string; name: string }) {
  const queries = useQueryClient();
  const principal = useQuery({ queryKey: ["principal"], queryFn: loadPrincipal });
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const close = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/admin/users/{xid}/close", {
        params: { path: { xid: userXid } },
        body: { reason },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setConfirming(false);
      setReason("");
      setError(null);
      void queries.invalidateQueries({ queryKey: ["members"] });
      void queries.invalidateQueries({ queryKey: ["cohort-members"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  if (!isPlatformAdmin(principal.data ?? null)) return null;

  if (!confirming) {
    return (
      <button className="link" onClick={() => setConfirming(true)}>
        Close account
      </button>
    );
  }

  return (
    <span className="row">
      <input value={reason} autoFocus
             placeholder="Why — usually the person asked"
             onChange={(event) => setReason(event.target.value)} />
      <button className="link"
              disabled={close.isPending || reason.trim() === ""}
              onClick={() => close.mutate()}>
        {close.isPending ? "Closing…" : "Confirm"}
      </button>
      <button className="link" onClick={() => setConfirming(false)}>Cancel</button>
      <span className="muted">
        {name} can no longer sign in, and every session ends now. Their attempts,
        results and recordings are kept — this closes the account, it does not
        erase the person&rsquo;s data.
      </span>
      {error && <span className="error">{error}</span>}
    </span>
  );
}
