/**
 * Assigning a published test to a cohort, and watching it being sat.
 *
 * This is the screen where a centre finds out whether it has paid for what it is
 * about to do, and there are **TWO gates, in this order** — established by
 * driving both against the API, after a first draft of this comment blamed the
 * wrong one:
 *
 *   1. `org.assignments` on the ORGANIZATION — may this centre set work at all.
 *      "No active entitlement for 'org.assignments'."
 *   2. `SEAT_BUNDLE` per TARGETED STUDENT — is each of them covered.
 *      "This centre's licence does not cover mock sittings…"
 *
 * Both answer `402 payment_required`, and the distinction matters because the
 * remedies are opposite: buying seats does nothing for the first, and renewing
 * the centre licence does nothing for the second. So the server's own message is
 * rendered verbatim and this screen does not guess which one it was — a UI that
 * said "buy seats" on a `org.assignments` refusal would send a centre to spend
 * money that changes nothing.
 *
 * The refusal is also WHOLE, never partial: covering some students and quietly
 * dropping the rest would split a class and leave a teacher wondering why eleven
 * of thirty cannot see the mock.
 *
 * Times are sent as instants. The server is the sole authority on time
 * remaining — a datetime-local input is in the browser's zone, so it is
 * converted with `toISOString()` and the window shown back is the server's.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";

export function Assignments() {
  const queries = useQueryClient();
  const [versionXid, setVersionXid] = useState("");
  const [cohortXid, setCohortXid] = useState("");
  const [opens, setOpens] = useState("");
  const [closes, setCloses] = useState("");
  const [mode, setMode] = useState<"exam" | "practice">("exam");
  const [review, setReview] = useState<"never" | "submit" | "close">("close");
  const [attempts, setAttempts] = useState("1");
  const [watching, setWatching] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const assignments = useQuery({
    queryKey: ["assignments"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/assignments", {
        params: { query: { limit: 50 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  // Only PUBLISHED versions can be assigned. Listing drafts would offer a choice
  // the server refuses, and "why can't I assign this?" is a support call.
  const tests = useQuery({
    queryKey: ["tests"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/tests", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

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
  const orgXid = orgs.data?.items?.[0]?.xid;

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

  const create = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/assignments", {
        body: {
          test_version_xid: versionXid,
          target_kind: "cohort",
          cohort_xid: cohortXid,
          opens_at: new Date(opens).toISOString(),
          closes_at: new Date(closes).toISOString(),
          mode,
          allow_review_after: review,
          max_attempts: Number(attempts) || 1,
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      void queries.invalidateQueries({ queryKey: ["assignments"] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const progress = useQuery({
    queryKey: ["assignment-progress", watching],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/assignments/{xid}/progress", {
        params: { path: { xid: watching! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(watching),
    // Five seconds, which the contract calls "the one legitimate realtime use
    // for exam state". Not a socket: an invigilator's screen going stale for a
    // few seconds costs nothing, and a poll cannot desynchronise.
    refetchInterval: 5000,
  });

  const assignable = (tests.data?.items ?? []).filter((t) => t.current_published_version_xid);

  return (
    <div className="page">
      <h1>Assignments</h1>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          create.mutate();
        }}
      >
        <label htmlFor="a-test">Published test</label>
        <select
          id="a-test"
          value={versionXid}
          onChange={(event) => setVersionXid(event.target.value)}
          required
        >
          <option value="">— choose a test —</option>
          {assignable.map((test) => (
            <option key={test.xid} value={test.current_published_version_xid!}>
              {test.title}
            </option>
          ))}
        </select>
        {tests.data && assignable.length === 0 && (
          <p className="muted">
            No published tests yet. A draft cannot be assigned — publish one from
            its composition screen first.
          </p>
        )}

        <label htmlFor="a-cohort">Cohort</label>
        <select
          id="a-cohort"
          value={cohortXid}
          onChange={(event) => setCohortXid(event.target.value)}
          required
        >
          <option value="">— choose a cohort —</option>
          {cohorts.data?.map((cohort) => (
            <option key={cohort.xid} value={cohort.xid}>
              {cohort.name} ({cohort.member_count} students)
            </option>
          ))}
        </select>

        <div className="row">
          <span>
            <label htmlFor="a-opens">Opens</label>
            <input
              id="a-opens"
              type="datetime-local"
              value={opens}
              onChange={(event) => setOpens(event.target.value)}
              required
            />
          </span>
          <span>
            <label htmlFor="a-closes">Closes</label>
            <input
              id="a-closes"
              type="datetime-local"
              value={closes}
              onChange={(event) => setCloses(event.target.value)}
              required
            />
          </span>
        </div>

        <label htmlFor="a-mode">Mode</label>
        <select
          id="a-mode"
          value={mode}
          onChange={(event) => setMode(event.target.value as "exam" | "practice")}
        >
          {/* Not cosmetic: exam mode enforces play-once on listening audio and
              the server-authoritative clock. Practice allows free replay. */}
          <option value="exam">Exam — timed, listening plays once</option>
          <option value="practice">Practice — replayable</option>
        </select>

        <label htmlFor="a-review">Students may review their answers</label>
        <select
          id="a-review"
          value={review}
          onChange={(event) =>
            setReview(event.target.value as "never" | "submit" | "close")
          }
        >
          {/* `submit` hands the paper back while classmates are still sitting it,
              which for a cohort assignment is how answers travel. `close` is the
              default for that reason. */}
          <option value="close">After the window closes (default)</option>
          <option value="submit">As soon as they submit</option>
          <option value="never">Never</option>
        </select>

        <label htmlFor="a-attempts">Attempts allowed</label>
        <input
          id="a-attempts"
          inputMode="numeric"
          value={attempts}
          onChange={(event) => setAttempts(event.target.value)}
        />

        <button disabled={create.isPending}>
          {create.isPending ? "Assigning…" : "Assign"}
        </button>
      </form>

      {error && (
        <>
          <p className="error">{error}</p>
          <p className="muted">
            A payment refusal here is one of two things and they have opposite
            remedies: the centre's own licence to set work
            (<code>org.assignments</code>), or seats for the individual students.
            The message above says which — it is the server's, not a guess.
          </p>
        </>
      )}

      <table>
        <thead>
          <tr><th>Test</th><th>Cohort</th><th>Window</th><th>Mode</th><th /></tr>
        </thead>
        <tbody>
          {assignments.data?.items?.map((assignment) => (
            <tr key={assignment.xid}>
              <td>{assignment.test_title}</td>
              <td>{assignment.cohort?.name ?? "—"}</td>
              <td className="muted">
                {new Date(assignment.opens_at).toLocaleString()} →{" "}
                {new Date(assignment.closes_at).toLocaleString()}
              </td>
              <td>{assignment.mode}</td>
              <td>
                <button
                  className="link"
                  onClick={() =>
                    setWatching(watching === assignment.xid ? null : assignment.xid)
                  }
                >
                  {watching === assignment.xid ? "Stop watching" : "Invigilate"}
                </button>
              </td>
            </tr>
          ))}
          {assignments.data?.items?.length === 0 && (
            <tr><td colSpan={5} className="muted">Nothing assigned yet.</td></tr>
          )}
        </tbody>
      </table>

      {watching && progress.data && (
        <div className="invigilate">
          <h2>Live progress</h2>
          <p className="muted">
            Server time {new Date(progress.data.server_now!).toLocaleTimeString()} ·
            refreshing every 5s
          </p>
          <table>
            <thead>
              <tr><th>Student</th><th>Status</th><th>Answered</th><th>Time left</th><th>Band</th></tr>
            </thead>
            <tbody>
              {progress.data.students?.map((row) => (
                <tr key={row.user?.xid}>
                  <td>{row.user?.given_name} {row.user?.family_name}</td>
                  <td>{row.status}</td>
                  <td className="muted">
                    {row.answered}/{row.total}
                  </td>
                  <td className="muted">
                    {/* Derived from the SERVER's `expires_at` against the
                        SERVER's `server_now`, never from the invigilator's
                        clock — a laptop three minutes fast would show a student
                        as out of time while they are still writing. */}
                    {row.expires_at && progress.data.server_now
                      ? `${Math.max(0, Math.round(
                          (new Date(row.expires_at).getTime() -
                            new Date(progress.data.server_now).getTime()) / 60000))} min`
                      : "—"}
                  </td>
                  <td>{row.band ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
