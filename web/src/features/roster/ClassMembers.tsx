/**
 * Who is in a class, and putting people in and out of it.
 *
 * The console could create a class and invite somebody straight into one, and
 * could do nothing else: an existing student could not be added, and nobody
 * could be removed. A class is the unit assignments target, so that made it a
 * thing you set up once at the moment a person joins the centre and then live
 * with — and classes change every term.
 *
 * **Removal is soft and it matters that it is.** `assignment_targets` rows
 * already written keep pointing at a real membership, so work a student was set
 * and the band they got still resolve. Taking somebody out of a class must not
 * rewrite what they have already sat.
 *
 * Only students are offered. A teacher is a member of the ORGANIZATION, and
 * putting one in a class would put them in the roster an assignment expands
 * into — the centre would be setting its own teacher a mock.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";

export function ClassMembers({ cohortXid, cohortName, orgXid, onClose }: {
  cohortXid: string;
  cohortName: string;
  orgXid: string;
  onClose: () => void;
}) {
  const queries = useQueryClient();
  const [adding, setAdding] = useState("");
  const [error, setError] = useState<string | null>(null);

  const members = useQuery({
    queryKey: ["cohort-members", cohortXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/cohorts/{xid}/members", {
        params: { path: { xid: cohortXid } },
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

  const refresh = () => {
    void queries.invalidateQueries({ queryKey: ["cohort-members", cohortXid] });
    // The class list shows a member count, which has just changed.
    void queries.invalidateQueries({ queryKey: ["cohorts", orgXid] });
  };

  const add = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/cohorts/{xid}/members", {
        params: { path: { xid: cohortXid } },
        // The endpoint takes a list and accepts up to a thousand. One at a time
        // here, because picking one name from a dropdown is what an admin
        // actually does between terms — a bulk move deserves its own control
        // rather than a multi-select nobody can undo.
        body: { user_xids: [adding] },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setAdding("");
      refresh();
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const remove = useMutation({
    mutationFn: async (userXid: string) => {
      const { error: failure } = await api.DELETE("/cohorts/{xid}/members/{user_xid}", {
        params: { path: { xid: cohortXid, user_xid: userXid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const inClass = new Set((members.data ?? []).map((m) => m.user?.xid));
  const addable = (people.data?.items ?? []).filter(
    (m) => m.role === "student" && m.user?.xid && !inClass.has(m.user.xid),
  );

  return (
    <div className="issued">
      <h2>{cohortName}</h2>
      {error && <p className="error">{error}</p>}

      <table>
        <thead>
          <tr><th>Name</th><th>Joined</th><th /></tr>
        </thead>
        <tbody>
          {members.data?.map((member) => (
            <tr key={member.user?.xid}>
              <td>
                {member.user?.given_name} {member.user?.family_name}
                {/* Shown when disclosed, never rendered as "adult" when absent:
                    absent means not disclosed, not false. A centre arranging
                    speaking practice needs it. */}
                {member.user?.is_minor === true && (
                  <span className="muted"> · under 18</span>
                )}
              </td>
              <td className="muted">
                {member.joined_at
                  ? new Date(member.joined_at).toLocaleDateString()
                  : "—"}
              </td>
              <td>
                <button
                  className="link"
                  onClick={() => member.user?.xid && remove.mutate(member.user.xid)}
                  disabled={remove.isPending}
                >
                  Remove
                </button>
              </td>
            </tr>
          ))}
          {members.data?.length === 0 && (
            <tr><td colSpan={3} className="muted">Nobody in this class yet.</td></tr>
          )}
        </tbody>
      </table>

      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          if (adding) add.mutate();
        }}
      >
        <select
          value={adding}
          onChange={(event) => setAdding(event.target.value)}
          aria-label="Student to add"
        >
          <option value="">— add a student —</option>
          {addable.map((member) => (
            <option key={member.user!.xid} value={member.user!.xid}>
              {member.user!.given_name} {member.user!.family_name}
            </option>
          ))}
        </select>
        <button disabled={add.isPending || !adding}>Add</button>
        <button type="button" className="link" onClick={onClose}>Close</button>
      </form>
      {people.data && addable.length === 0 && (
        <p className="muted">
          Every student at this centre is already in this class. Somebody who has
          not joined yet needs an invitation first — a person must belong to the
          centre before they can belong to one of its classes.
        </p>
      )}
      <p className="muted">
        Removing somebody takes them off future assignments for this class and
        leaves what they have already sat exactly as it is.
      </p>
    </div>
  );
}
