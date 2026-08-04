/**
 * The centre: its settings, its people, its classes, and invitations.
 *
 * Three things here are load-bearing and were previously unreachable from any
 * screen, which meant a centre could not actually be set up through the console.
 *
 * **The org settings are policy, not preferences.** `teacher_can_publish` and
 * `content_edit_others` are the only two ways a teacher gains a permission the
 * matrix denies by default, and `require_review` is the only way a second pair of
 * eyes becomes mandatory before publishing. All three default OFF, and that is
 * deliberate — most centres here are one or two people, and a universal review
 * bar plus the no-self-approval rule is a one-teacher centre that cannot publish
 * at all. Turning `require_review` on is a centre ASSERTING it has two people.
 *
 * **An invite's token is shown once.** Only its hash is stored, so this response
 * is the only place the raw token ever exists. Nothing in this product delivers
 * an invite — `delivered_via` is a hardcoded string with no sender behind it —
 * so the admin copies it and sends it, or the invitee finds the invitation on
 * `GET /invites/pending` against their own confirmed number.
 *
 * **An invite is bound to the phone it names.** Forwarding the link no longer
 * transfers the role, which is why the number matters more than it looks: a
 * typo'd digit is an invitation nobody can accept.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { ClassMembers } from "./ClassMembers";
import { Seats } from "./Seats";

const ROLES = ["student", "teacher", "centre_admin"] as const;

export function Roster() {
  const queries = useQueryClient();
  const [phone, setPhone] = useState("+998");
  const [role, setRole] = useState<(typeof ROLES)[number]>("student");
  const [inviteCohort, setInviteCohort] = useState("");
  const [issued, setIssued] = useState<{ xid: string; token: string } | null>(null);
  const [cohortName, setCohortName] = useState("");
  const [openClass, setOpenClass] =
    useState<{ xid: string; name: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const orgs = useQuery({
    queryKey: ["orgs"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs", {
        params: { query: { limit: 25 } },
      });
      if (failure) throw failure;
      return data;
    },
  });
  const listed = orgs.data?.items?.[0];
  const orgXid = listed?.xid;

  // The single-org read, not the listing, is what the settings below are drawn
  // from. Both carry `settings`, so this is not working around a gap — it is
  // that the PATCH is last-write-wins between two admins, and re-reading the
  // one org afterwards shows what the server actually stored rather than what
  // this browser sent. Falls back to the listing so the page renders on the
  // first paint, before the detail arrives.
  const detail = useQuery({
    queryKey: ["org", orgXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs/{xid}", {
        params: { path: { xid: orgXid! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(orgXid),
  });
  const org = detail.data ?? listed;

  const members = useQuery({
    queryKey: ["members", orgXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs/{xid}/members", {
        params: { path: { xid: orgXid! }, query: { limit: 200 } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(orgXid),
  });

  const cohorts = useQuery({
    queryKey: ["cohorts", orgXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs/{xid}/cohorts", {
        params: { path: { xid: orgXid! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(orgXid),
  });

  const setSetting = useMutation({
    // All three sent every time, not just the one toggled. `OrgSettings` declares
    // them required, so a partial object does not satisfy the contract — the
    // server happens to MERGE (`{**existing, **incoming}`), which makes partial
    // work at runtime, but writing to that would be coding against an
    // implementation detail the contract does not promise.
    //
    // The cost is last-write-wins between two admins editing at once. Acceptable
    // at this size and worth naming: these are three booleans changed rarely, not
    // a document two people co-edit.
    mutationFn: async (change: Partial<Record<string, boolean>>) => {
      const current = (org?.settings ?? {}) as Record<string, unknown>;
      const { error: failure } = await api.PATCH("/orgs/{xid}", {
        params: { path: { xid: orgXid! } },
        body: {
          settings: {
            teacher_can_publish: current["teacher_can_publish"] === true,
            content_edit_others: current["content_edit_others"] === true,
            require_review: current["require_review"] === true,
            ...change,
          },
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      void queries.invalidateQueries({ queryKey: ["orgs"] });
      void queries.invalidateQueries({ queryKey: ["org", orgXid] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const invite = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/orgs/{xid}/invites", {
        params: { path: { xid: orgXid! } },
        body: {
          phone,
          role,
          ...(inviteCohort ? { cohort_xid: inviteCohort } : {}),
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      // Held in state because it is unrecoverable: only the hash is stored, so
      // navigating away without copying means revoking and reissuing.
      if (data?.token) setIssued({ xid: data.xid!, token: data.token });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const revoke = useMutation({
    mutationFn: async (inviteXid: string) => {
      const { error: failure } = await api.DELETE("/orgs/{xid}/invites/{invite_xid}", {
        params: { path: { xid: orgXid!, invite_xid: inviteXid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => setIssued(null),
    onError: (failure) => setError(problemText(failure)),
  });

  const addCohort = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/orgs/{xid}/cohorts", {
        params: { path: { xid: orgXid! } },
        body: { name: cohortName.trim() },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setCohortName("");
      void queries.invalidateQueries({ queryKey: ["cohorts", orgXid] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const settings = (org?.settings ?? {}) as Record<string, unknown>;

  if (orgs.isPending) return <div className="page muted">Loading…</div>;
  if (!org) {
    return (
      <div className="page">
        <h1>Centre</h1>
        <p className="muted">
          You are not a member of any organization. Only a platform admin can
          create one.
        </p>
      </div>
    );
  }

  return (
    <div className="page">
      <h1>{org.name}</h1>
      <p className="muted">{org.kind} · {org.status}</p>
      {error && <p className="error">{error}</p>}

      <h2>Policy</h2>
      <fieldset>
        <legend>What this centre allows</legend>
        <label className="choice">
          <input
            type="checkbox"
            checked={settings["teacher_can_publish"] === true}
            onChange={(e) => setSetting.mutate({ teacher_can_publish: e.target.checked })}
          />
          Teachers may publish tests
          <span className="muted"> — off by default; publishing is a centre-admin act</span>
        </label>
        <label className="choice">
          <input
            type="checkbox"
            checked={settings["content_edit_others"] === true}
            onChange={(e) => setSetting.mutate({ content_edit_others: e.target.checked })}
          />
          Teachers may edit each other's drafts
        </label>
        <label className="choice">
          <input
            type="checkbox"
            checked={settings["require_review"] === true}
            onChange={(e) => setSetting.mutate({ require_review: e.target.checked })}
          />
          A version must be approved before it can be published
          <span className="muted">
            {" "}— nobody may approve their own, so turning this on asserts this
            centre has two people
          </span>
        </label>
      </fieldset>

      <h2>Invite someone</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setIssued(null);
          invite.mutate();
        }}
      >
        <label htmlFor="i-phone">Phone number</label>
        <input
          id="i-phone"
          value={phone}
          onChange={(event) => setPhone(event.target.value)}
          pattern="^\+998[0-9]{9}$"
          required
        />
        <p className="muted">
          The invitation is bound to this number — only this person can accept it,
          and forwarding the link does not transfer the role. A wrong digit is an
          invitation nobody can accept.
        </p>

        <label htmlFor="i-role">Role</label>
        <select
          id="i-role"
          value={role}
          onChange={(event) => setRole(event.target.value as (typeof ROLES)[number])}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>{r.replaceAll("_", " ")}</option>
          ))}
        </select>

        <label htmlFor="i-cohort">Also add to a class (optional)</label>
        <select
          id="i-cohort"
          value={inviteCohort}
          onChange={(event) => setInviteCohort(event.target.value)}
        >
          <option value="">— none —</option>
          {cohorts.data?.map((cohort) => (
            <option key={cohort.xid} value={cohort.xid}>{cohort.name}</option>
          ))}
        </select>

        <button disabled={invite.isPending}>
          {invite.isPending ? "Creating…" : "Create invitation"}
        </button>
      </form>

      {issued && (
        <div className="issued">
          <p>
            <strong>Copy this now.</strong> Only its hash is stored, so it cannot
            be shown again — if you lose it, revoke and issue another.
          </p>
          <code className="token">{issued.token}</code>
          <div className="row">
            <button
              type="button"
              onClick={() => void navigator.clipboard?.writeText(issued.token)}
            >
              Copy
            </button>
            <button
              type="button"
              className="link"
              onClick={() => revoke.mutate(issued.xid)}
              disabled={revoke.isPending}
            >
              Revoke it
            </button>
          </div>
          <p className="muted">
            Nothing sends this automatically. Send it to them, or tell them to
            sign in and look under their pending invitations — it is waiting
            against their number either way.
          </p>
        </div>
      )}

      <h2>Classes</h2>
      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          if (cohortName.trim()) addCohort.mutate();
        }}
      >
        <input
          value={cohortName}
          onChange={(event) => setCohortName(event.target.value)}
          placeholder="Evening IELTS"
          aria-label="New class name"
        />
        <button disabled={addCohort.isPending || !cohortName.trim()}>Add class</button>
      </form>
      <ul className="tree">
        {cohorts.data?.map((cohort) => (
          <li key={cohort.xid}>
            {cohort.name}{" "}
            <span className="muted">
              · {cohort.member_count} student{cohort.member_count === 1 ? "" : "s"}
            </span>{" "}
            <button
              className="link"
              onClick={() =>
                setOpenClass(
                  openClass?.xid === cohort.xid
                    ? null
                    : { xid: cohort.xid, name: cohort.name },
                )
              }
            >
              {openClass?.xid === cohort.xid ? "Close" : "Who's in it"}
            </button>
          </li>
        ))}
        {cohorts.data?.length === 0 && <li className="muted">No classes yet.</li>}
      </ul>

      {openClass && orgXid && (
        <ClassMembers
          cohortXid={openClass.xid}
          cohortName={openClass.name}
          orgXid={orgXid}
          onClose={() => setOpenClass(null)}
        />
      )}

      {orgXid && <Seats orgXid={orgXid} />}

      <h2>People</h2>
      <table>
        <thead>
          <tr><th>Name</th><th>Role</th><th>Status</th><th>Joined</th></tr>
        </thead>
        <tbody>
          {members.data?.items?.map((m) => (
            <tr key={m.user?.xid}>
              <td>
                {m.user?.given_name} {m.user?.family_name}
                {/* `is_minor` is null for anyone this actor may not see it for.
                    Shown when present because a centre admin arranging speaking
                    practice needs it, and never rendered as "adult" when absent —
                    absent means not disclosed, not false. */}
                {m.user?.is_minor === true && (
                  <span className="muted"> · under 18</span>
                )}
              </td>
              <td>{m.role}</td>
              <td className="muted">{m.status}</td>
              <td className="muted">
                {m.joined_at ? new Date(m.joined_at).toLocaleDateString() : "—"}
              </td>
            </tr>
          ))}
          {members.data?.items?.length === 0 && (
            <tr><td colSpan={4} className="muted">Nobody yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
