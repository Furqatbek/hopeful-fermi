/**
 * The centre's licence, and who is covered by it.
 *
 * This screen exists because of a refusal on another one. Assigning a mock can
 * fail with `402 payment_required` for **two different reasons with opposite
 * remedies** — the centre's own licence to set work at all (`org.assignments`),
 * or a seat for each targeted student — and the Assignments screen renders the
 * server's message verbatim rather than guessing which. That was the right call
 * and it left an admin with a sentence and nowhere to act on it.
 *
 * So both gates are shown here, separately, in the order the server checks them.
 *
 * **A seat is spent until it is taken back.** The licence covers a student only
 * while they hold an unreleased seat, so a leaver holds one until somebody
 * releases it — on a ten-seat licence that is a centre stuck at the first ten
 * students it ever seated.
 *
 * Nothing here takes payment. `POST /orders` returns a provider redirect and
 * that is a flow of its own; what a centre needs first is to see what it holds
 * and to move the seats it has already paid for.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";

export function Seats({ orgXid }: { orgXid: string }) {
  const queries = useQueryClient();
  const [adding, setAdding] = useState("");
  const [error, setError] = useState<string | null>(null);

  const seats = useQuery({
    queryKey: ["seats", orgXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs/{xid}/seats", {
        params: { path: { xid: orgXid } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const people = useQuery({
    queryKey: ["members", orgXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs/{xid}/members", {
        params: { path: { xid: orgXid }, query: { limit: 200 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const refresh = () => queries.invalidateQueries({ queryKey: ["seats", orgXid] });

  const assign = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/orgs/{xid}/seats", {
        params: { path: { xid: orgXid } },
        body: { user_xids: [adding] },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setAdding("");
      void refresh();
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const release = useMutation({
    mutationFn: async (userXid: string) => {
      const { error: failure } = await api.DELETE("/orgs/{xid}/seats/{user_xid}", {
        params: { path: { xid: orgXid, user_xid: userXid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      void refresh();
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const summary = seats.data;
  const seated = new Set((summary?.members ?? []).map((m) => m.xid));
  const seatable = (people.data?.items ?? []).filter(
    (m) => m.role === "student" && m.user?.xid && !seated.has(m.user.xid),
  );
  const expiring = summary?.expires_at ? new Date(summary.expires_at) : null;
  const noLicence = !summary?.entitlement_xid;

  return (
    <>
      <h2>Seats</h2>
      {error && <p className="error">{error}</p>}

      {noLicence ? (
        <p className="muted">
          {/* Zero rather than a 404, which is the true answer to "how many seats
              do we have". */}
          This centre holds no seat licence, so no student is covered for a mock
          set by the centre. That is one of the two things a{" "}
          <code>402</code> on the Assignments screen means; the other is the
          centre's own licence to set work at all, which is separate and is not
          bought by adding seats.
        </p>
      ) : (
        <>
          <p>
            <strong>{summary.assigned} of {summary.total}</strong> seats in use ·{" "}
            <strong>{summary.remaining}</strong> free
            {expiring && (
              <span className="muted">
                {" "}· licence runs to {expiring.toLocaleDateString()}
              </span>
            )}
          </p>
          {summary.remaining === 0 && (
            <p className="error">
              Every seat is taken. Assigning a mock to a student without one is
              refused — release a seat from somebody who has left, or buy more.
            </p>
          )}

          <div className="scroll">
            <table>
              <thead>
                <tr><th>Student</th><th>Phone</th><th /></tr>
              </thead>
              <tbody>
                {summary.members?.map((member) => (
                  <tr key={member.xid}>
                    <td>{member.given_name} {member.family_name}</td>
                    <td className="muted">{member.phone}</td>
                    <td>
                      <button
                        className="link"
                        onClick={() => member.xid && release.mutate(member.xid)}
                        disabled={release.isPending}
                      >
                        Release
                      </button>
                    </td>
                  </tr>
                ))}
                {summary.members?.length === 0 && (
                  <tr><td colSpan={3} className="muted">No seats assigned yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <form
            className="row"
            onSubmit={(event) => {
              event.preventDefault();
              setError(null);
              if (adding) assign.mutate();
            }}
          >
            <select
              value={adding}
              onChange={(event) => setAdding(event.target.value)}
              aria-label="Student to seat"
            >
              <option value="">— give a seat to —</option>
              {seatable.map((member) => (
                <option key={member.user!.xid} value={member.user!.xid}>
                  {member.user!.given_name} {member.user!.family_name}
                </option>
              ))}
            </select>
            <button disabled={assign.isPending || !adding}>Assign</button>
          </form>

          <p className="muted">
            Releasing a seat frees it immediately and stops covering that student.
            When each seat was held, and by whom, is kept — it is what a billing
            question is settled with.
          </p>
        </>
      )}
    </>
  );
}
