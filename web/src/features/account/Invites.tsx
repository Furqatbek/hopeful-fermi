/**
 * Invitations sent to my phone number, and redeeming one.
 *
 * **The phone number is the delivery channel, because nothing else is.**
 * `POST /orgs/{xid}/invites` hands the raw token back to the admin who created
 * it and hardcodes `delivered_via: "telegram"` with no sender behind it, so a
 * centre onboarding forty teachers has forty numbers and no way to reach any of
 * them. The remedy the backend built is this: sign in, confirm the number, and
 * the invitations addressed to it are here to accept. It is also what stopped
 * the tokens being pasted into group chats.
 *
 * **An invite is bound to the number it names.** Redemption checks that the
 * number on the invite is the caller's own confirmed number, so a forwarded
 * `centre_admin` link no longer makes a centre admin of whoever opens it first.
 * That is why the token box below can legitimately be refused: holding the token
 * is not the same as being the person it was sent to.
 *
 * **Exactly one of `token` or `xid`.** The request model rejects both and
 * neither, so this screen never assembles a body with two keys — the two paths
 * are separate submits, not one form with two optional inputs.
 *
 * Accepting changes what this person may do. `forgetPrincipal` is called on
 * success because `principal.ts` caches roles in a module-level variable for the
 * life of the page, and a teacher who has just joined a centre would otherwise
 * keep the navigation of somebody who belongs to nothing until they reload.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { type Problem, api, problemText } from "../../api/client";
import { forgetPrincipal } from "../../api/principal";

export function Invites() {
  const queries = useQueryClient();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [joined, setJoined] = useState<{ org: string; role: string } | null>(null);

  const pending = useQuery({
    queryKey: ["pending-invites"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/invites/pending");
      if (failure) throw failure;
      return data;
    },
  });

  const accepted = (data: { org?: { name?: string }; role?: string } | undefined) => {
    setError(null);
    setToken("");
    setJoined({ org: data?.org?.name ?? "the organization", role: data?.role ?? "" });
    // Roles and memberships have changed. The cached principal decides which
    // controls the console draws; leaving it stale would show the person the
    // navigation they had a moment ago.
    forgetPrincipal();
    void queries.invalidateQueries({ queryKey: ["pending-invites"] });
    void queries.invalidateQueries({ queryKey: ["orgs"] });
    void queries.invalidateQueries({ queryKey: ["principal"] });
  };

  const acceptByXid = useMutation({
    mutationFn: async (xid: string) => {
      const { data, error: failure } = await api.POST("/invites/accept", {
        body: { xid },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: accepted,
    onError: (failure) => setError(problemText(failure)),
  });

  const acceptByToken = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/invites/accept", {
        body: { token: token.trim() },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: accepted,
    onError: (failure) => setError(problemText(failure)),
  });

  // Read off the problem document rather than the status code. A 403 here is
  // either "your number is not confirmed" or "this invitation is somebody
  // else's", and those have completely different remedies — the first is a state
  // every new staff account passes through, the second is a refusal.
  const unverified =
    (pending.error as Problem | null)?.code === "phone_not_verified";

  return (
    <div className="page">
      <h1>Invitations</h1>
      {error && <p className="error">{error}</p>}

      {joined && (
        <div className="issued">
          <p>
            You joined <strong>{joined.org}</strong>
            {joined.role && <> as <strong>{joined.role.replaceAll("_", " ")}</strong></>}.
          </p>
          <p className="muted">
            What you can see and do changed with it. If a page still looks the
            way it did a moment ago, reload it.
          </p>
        </div>
      )}

      {unverified ? (
        <p className="muted">
          {/* Not an error state. `GET /invites/pending` refuses until an SMS code
              has proved the number, because an invitation is matched against it
              and `users.phone` on its own is a self-declared string that
              registration takes from the client. */}
          Invitations are matched against your phone number, and it has to be
          confirmed by a code before that match is trusted. Sign out and sign in
          again with a code sent to your number, then come back here. Until then
          this list stays empty even if somebody has invited you.
        </p>
      ) : (
        <>
          {pending.isPending && <p className="muted">Loading…</p>}
          {pending.isError && !unverified && (
            <p className="error">{problemText(pending.error)}</p>
          )}
          <div className="scroll">
            <table>
              <thead>
                <tr><th>Organization</th><th>Role</th><th>Expires</th><th /></tr>
              </thead>
              <tbody>
                {pending.data?.map((invite) => (
                  <tr key={invite.xid}>
                    <td>{invite.org.name}</td>
                    <td>{invite.role.replaceAll("_", " ")}</td>
                    <td className="muted">
                      {invite.expires_at
                        ? new Date(invite.expires_at).toLocaleDateString()
                        : "—"}
                    </td>
                    <td>
                      <button
                        onClick={() => {
                          setError(null);
                          acceptByXid.mutate(invite.xid);
                        }}
                        disabled={acceptByXid.isPending}
                      >
                        Accept
                      </button>
                    </td>
                  </tr>
                ))}
                {pending.data?.length === 0 && (
                  <tr>
                    <td colSpan={4} className="muted">
                      Nothing is waiting for your number.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="muted">
            Spent, withdrawn and expired invitations are not listed. An
            invitation runs for fourteen days from when it was created; after
            that the centre has to issue another.
          </p>
        </>
      )}

      <h2>Have a link instead</h2>
      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          if (token.trim()) acceptByToken.mutate();
        }}
      >
        <input
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="Paste the invitation token"
          aria-label="Invitation token"
        />
        <button disabled={acceptByToken.isPending || !token.trim()}>
          {acceptByToken.isPending ? "Joining…" : "Use this token"}
        </button>
      </form>
      <p className="muted">
        The token still has to have been sent to your own confirmed number.
        Somebody else's invitation is refused even with the token in hand, which
        is what stops a forwarded link handing over a role.
      </p>
    </div>
  );
}
