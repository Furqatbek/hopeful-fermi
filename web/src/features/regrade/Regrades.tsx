/**
 * Fixing a bad answer key, and regrading what it already scored.
 *
 * "Regrade is not optional — bad keys are the fastest way to lose a school
 * client." This is that flow, and its shape is deliberate: **fix, look, apply.**
 *
 * **Fixing the key stages the regrade in the same request.** The server returns
 * `regrade_job_xid` alongside the new key version, so the correction and its
 * consequence arrive together. This screen never asks an author to go and stage
 * one afterwards, because the failure mode of that design is silence: the key is
 * right, the author believes they are finished, and every student who already sat
 * the item keeps the band the wrong key gave them.
 *
 * **A key fix creates a NEW key version; it does not edit the old one.** A test
 * scored against v1 keeps meaning what it meant, and the regrade is what moves
 * attempts onto v2. That is why `reason` is `key_fix` rather than `initial`.
 *
 * **Staged is not applied, and `ready` is not `planning`.** The dry run reports
 * how many attempts it touches and how many BANDS move; band movement is computed
 * by a worker, so a job is `planning` until that lands and `apply` refuses
 * anything that is not `ready`. The apply control follows the server's status
 * rather than a spinner of our own — a button that looks ready before the numbers
 * exist is a button somebody presses.
 *
 * **A finished competition blocks apply outright.** Not a warning: `apply`
 * answers 409 `competition_decision_required` and names the contests. A published
 * ranking is the entire product of a contest, and only a platform admin may
 * decide, through `POST /competitions/{xid}/regrade-decisions/{job_xid}`. Centre
 * staff cannot, so this screen says who can rather than offering a control that
 * would refuse them.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";

/** Statuses where the planner has finished and the numbers are real. */
const SETTLED = ["ready", "running", "completed", "failed", "cancelled"];

export function Regrades() {
  const queries = useQueryClient();
  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });
  const admin = isPlatformAdmin(principal.data ?? null);
  const [questionVersionXid, setQuestionVersionXid] = useState("");
  const [keyText, setKeyText] = useState("");
  const [note, setNote] = useState("");
  const [opened, setOpened] = useState<string | null>(null);
  const [reviewed, setReviewed] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const questions = useQuery({
    queryKey: ["questions"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/questions", {
        params: { query: { limit: 200 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  // A bare array, and its only filter is `status` — there is no `limit` here.
  const jobs = useQuery({
    queryKey: ["regrades"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/regrades");
      if (failure) throw failure;
      return data;
    },
    // A job sits in `planning` until the worker computes band movement. Polling
    // is how the Apply control becomes live without the admin reloading; five
    // seconds because the alternative is a stale screen that looks broken.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((job) => job.status === "planning") ? 5000 : false,
  });

  const impact = useQuery({
    queryKey: ["regrade", opened],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/regrades/{xid}", {
        params: { path: { xid: opened! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(opened),
    refetchInterval: (query) => (query.state.data?.status === "planning" ? 5000 : false),
  });

  const fixKey = useMutation({
    mutationFn: async () => {
      let key: Record<string, unknown>;
      try {
        key = JSON.parse(keyText);
      } catch {
        throw new Error("The corrected key is not valid JSON.");
      }
      const { data, error: failure } = await api.POST("/question-versions/{xid}/keys", {
        params: { path: { xid: questionVersionXid } },
        body: {
          key,
          // Not `initial`. A `key_fix` is what the regrade flow keys off, and
          // mislabelling a correction as a first key would suggest there was
          // nothing to put right.
          reason: "key_fix",
          ...(note.trim() ? { note: note.trim() } : {}),
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      setKeyText("");
      setNote("");
      // Straight into the impact report. Fixing without looking at what it moves
      // is the one thing this flow exists to prevent.
      if (data?.regrade_job_xid) setOpened(data.regrade_job_xid);
      void queries.invalidateQueries({ queryKey: ["regrades"] });
      void queries.invalidateQueries({ queryKey: ["questions"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const apply = useMutation({
    mutationFn: async (xid: string) => {
      const { error: failure } = await api.POST("/regrades/{xid}/apply", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      void queries.invalidateQueries({ queryKey: ["regrades"] });
      void queries.invalidateQueries({ queryKey: ["regrade", opened] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const withVersions = (questions.data?.items ?? []).filter((q) => q.current_version?.xid);
  const job = impact.data;
  const contests = job?.impact?.competition_impact ?? [];
  const blocked = contests.some((c) => c.decision_required);
  const settled = job ? SETTLED.includes(job.status) : false;

  return (
    <div className="page">
      <h1>Keys and regrades</h1>
      {error && <p className="error">{error}</p>}

      <h2>Correct an answer key</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          fixKey.mutate();
        }}
      >
        <label htmlFor="r-q">Question</label>
        <select
          id="r-q"
          value={questionVersionXid}
          onChange={(event) => setQuestionVersionXid(event.target.value)}
          required
        >
          <option value="">— choose a question —</option>
          {withVersions.map((q) => (
            <option key={q.xid} value={q.current_version!.xid}>
              {q.type_key} · v{q.current_version!.version_no} ·{" "}
              {q.current_version!.slot_keys?.join(", ") || "no slots"}
            </option>
          ))}
        </select>

        <label htmlFor="r-key">Corrected key (JSON)</label>
        <textarea
          id="r-key"
          rows={4}
          value={keyText}
          onChange={(event) => setKeyText(event.target.value)}
          placeholder={'{"slots": {"s1": {"accept": ["fourteen", "14"]}}}'}
          required
        />

        <label htmlFor="r-note">What was wrong</label>
        <input
          id="r-note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="Accepted spelling 'metres' was missing"
        />
        <p className="muted">
          Recorded on the key version and carried into the regrade's reason, so the
          audit trail says why the band moved, not just that it did.
        </p>

        <p className="muted">
          This creates a NEW key version — the old one is not edited, so anything
          already scored keeps meaning what it meant. If the item has been sat, a
          regrade is staged as a <strong>dry run</strong> at the same time. Nothing
          is rescored until you read the impact below and apply it.
        </p>

        <button disabled={fixKey.isPending || !questionVersionXid}>
          {fixKey.isPending ? "Saving…" : "Save corrected key"}
        </button>
      </form>

      <h2>Regrade jobs</h2>
      <table>
        <thead>
          <tr><th>Trigger</th><th>Status</th><th>Attempts</th><th>Created</th><th /></tr>
        </thead>
        <tbody>
          {jobs.data?.map((row) => (
            <tr key={row.xid}>
              <td>{row.trigger ?? "—"}</td>
              <td>
                {row.status}
                {row.dry_run && <span className="muted"> · not applied</span>}
              </td>
              <td className="muted">{row.impact?.attempts_total ?? "—"}</td>
              <td className="muted">
                {row.created_at ? new Date(row.created_at).toLocaleString() : "—"}
              </td>
              <td>
                <button
                  className="link"
                  onClick={() => setOpened(opened === row.xid ? null : row.xid)}
                >
                  {opened === row.xid ? "Hide" : "Impact"}
                </button>
              </td>
            </tr>
          ))}
          {jobs.data?.length === 0 && (
            <tr><td colSpan={5} className="muted">No regrades yet.</td></tr>
          )}
        </tbody>
      </table>

      {job && (
        <div className="issued">
          <h2>Impact</h2>
          {!settled ? (
            <p className="muted">
              Working out what this would change. Band movement is computed in the
              background — a popular question can carry thousands of sat attempts —
              so the numbers appear here shortly. Nothing has been rescored.
            </p>
          ) : (
            <>
              <table>
                <tbody>
                  <tr>
                    <td>Attempts in scope</td>
                    <td>{job.impact?.attempts_total ?? 0}</td>
                  </tr>
                  <tr>
                    <td>Raw scores that would change</td>
                    <td>{job.impact?.scores_changed ?? 0}</td>
                  </tr>
                  <tr>
                    <td><strong>Bands that would change</strong></td>
                    <td><strong>{job.impact?.bands_changed ?? 0}</strong></td>
                  </tr>
                  <tr>
                    <td>Students notified</td>
                    <td>
                      {job.impact?.students_to_notify ?? 0}
                      {/* The same number as `bands_changed`, and deliberately so:
                          only a band change is notified. A raw-score wobble that
                          leaves the band alone is not news, and messaging everyone
                          for one trains students to ignore the message that
                          matters. Shown as its own row because "who gets told" is
                          a different question from "what moved", even when the
                          answer happens to be the same. */}
                      <span className="muted"> — band changes only</span>
                    </td>
                  </tr>
                </tbody>
              </table>

              {blocked && (
                <>
                  <p className="error">
                    Blocked: this touches{" "}
                    {contests.length === 1 ? "a finished contest" : "finished contests"} —{" "}
                    {contests.map((c) => c.title ?? c.competition_xid).join(", ")}. Applying
                    will be refused until a <strong>platform admin</strong> records a
                    decision for each. A published ranking is the product of a contest;
                    it does not re-rank itself because a key moved.
                  </p>
                  {admin
                    ? contests
                        .filter((c) => c.decision_required && c.competition_xid)
                        .map((contest) => (
                          <ContestDecision
                            key={contest.competition_xid}
                            competitionXid={contest.competition_xid!}
                            title={contest.title ?? contest.competition_xid!}
                            jobXid={job.xid}
                            onDecided={() => {
                              void queries.invalidateQueries({
                                queryKey: ["regrade", opened],
                              });
                            }}
                          />
                        ))
                    : (
                      /* Not a disabled button: a centre admin can never do this,
                         and the reason is not that they have not earned it. The
                         centre whose students are ranked is not the party to
                         decide whether their ranking moves. */
                      <p className="muted">
                        Only a platform admin can decide this. Send them the job
                        reference <code>{job.xid}</code>.
                      </p>
                    )}
                </>
              )}

              {job.dry_run && !blocked && job.status === "ready" && (
                <>
                  <label className="choice">
                    <input
                      type="checkbox"
                      checked={reviewed.has(job.xid)}
                      onChange={(event) =>
                        setReviewed((previous) => {
                          const next = new Set(previous);
                          if (event.target.checked) next.add(job.xid);
                          else next.delete(job.xid);
                          return next;
                        })
                      }
                    />
                    I have read these numbers
                  </label>
                  <button
                    onClick={() => apply.mutate(job.xid)}
                    /* The confirmation is the point of the whole flow, not
                       ceremony: this rewrites bands on finished exams, and
                       `bands_changed` is the number a teacher has to be willing
                       to defend to that many students. */
                    disabled={apply.isPending || !reviewed.has(job.xid)}
                  >
                    {apply.isPending ? "Applying…" : "Apply this regrade"}
                  </button>
                </>
              )}

              {!job.dry_run && (
                <p className="muted">
                  Applied. Previous score runs are kept, so each student's history
                  still shows what they were originally marked.
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}


/**
 * What a key fix does to a contest that has already been ranked.
 *
 * Two answers and no third. `leave_as_is` keeps the published board and records
 * why — which is often right, because a ranking people have already screenshotted
 * has a life outside this database. `regrade_and_republish` recomputes it and
 * REQUIRES a public notice: if a podium moves, the people on it are told, in
 * writing, of the decision that moved them. A ranking that changes quietly is
 * worse than one that was wrong.
 *
 * The rationale is required either way, because this row is the answer to "who
 * decided, and on what grounds" — and for `leave_as_is` it is the only answer
 * there will ever be, since nothing else about the contest changes.
 */
function ContestDecision({ competitionXid, title, jobXid, onDecided }: {
  competitionXid: string;
  title: string;
  jobXid: string;
  onDecided: () => void;
}) {
  const [decision, setDecision] =
    useState<"leave_as_is" | "regrade_and_republish">("leave_as_is");
  const [rationale, setRationale] = useState("");
  const [notice, setNotice] = useState("");
  const [failed, setFailed] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const decide = useMutation({
    mutationFn: async () => {
      if (!rationale.trim()) {
        throw new Error("Say why. This row is the record of who decided and on "
                        + "what grounds.");
      }
      const { error: failure } = await api.POST(
        "/competitions/{xid}/regrade-decisions/{job_xid}",
        {
          params: { path: { xid: competitionXid, job_xid: jobXid } },
          body: {
            decision,
            rationale: rationale.trim(),
            ...(decision === "regrade_and_republish"
              ? { public_notice: notice.trim() }
              : {}),
          },
        },
      );
      if (failure) throw failure;
    },
    onSuccess: () => {
      setFailed(null);
      setDone(true);
      onDecided();
    },
    onError: (failure) => setFailed(problemText(failure) || String(failure)),
  });

  if (done) {
    return (
      <p className="muted">
        Decision recorded for <strong>{title}</strong>. Apply again to run the
        regrade.
      </p>
    );
  }

  return (
    <fieldset>
      <legend>Decide: {title}</legend>
      {failed && <p className="error">{failed}</p>}
      <label className="choice">
        <input
          type="radio"
          name={`decision-${competitionXid}`}
          checked={decision === "leave_as_is"}
          onChange={() => setDecision("leave_as_is")}
        />
        Leave the published ranking as it is
      </label>
      <label className="choice">
        <input
          type="radio"
          name={`decision-${competitionXid}`}
          checked={decision === "regrade_and_republish"}
          onChange={() => setDecision("regrade_and_republish")}
        />
        Recompute it and republish
      </label>

      <label htmlFor={`why-${competitionXid}`}>Why</label>
      <textarea
        id={`why-${competitionXid}`}
        rows={2}
        value={rationale}
        onChange={(event) => setRationale(event.target.value)}
        placeholder="Two ranks move below the podium; the key fix does not change the top three."
      />

      {decision === "regrade_and_republish" && (
        <>
          <label htmlFor={`notice-${competitionXid}`}>
            Public notice (required)
          </label>
          <textarea
            id={`notice-${competitionXid}`}
            rows={2}
            value={notice}
            onChange={(event) => setNotice(event.target.value)}
            placeholder="An answer key was corrected after this contest; the results below have been recomputed."
          />
          <p className="muted">
            Shown to entrants with the new board. Republishing without one is
            refused — a podium that moves without explanation is the version of
            this that loses a school.
          </p>
        </>
      )}

      <button onClick={() => decide.mutate()} disabled={decide.isPending}>
        {decide.isPending ? "Recording…" : "Record this decision"}
      </button>
    </fieldset>
  );
}
