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
 *
 * **Some regrades have no key fix to hang on.** A retuned band map moves every
 * band scored against that curve and touches no key at all; an engine fix moves
 * marking across every paper. `POST /regrades` stages those by hand — and stages
 * them as the same dry run, into the same impact report, behind the same
 * confirmation. Nothing here rescores anything: manual staging that skipped the
 * review step would be a one-click rewrite of finished exams, which is precisely
 * what the rest of this screen is built to prevent.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AnswerKeyEditor } from "../questions/AnswerKeyEditor";
import { type KeyValue, controlFor, emptyValue, fromKey, slotCountOf, slotIds, toKey }
  from "../questions/answerKey";
import { useState } from "react";

import { Confirm } from "../../app/Confirm";
import { Status } from "../../app/Icon";
import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";

/** Statuses where the planner has finished and the numbers are real. */
const SETTLED = ["ready", "running", "completed", "failed", "cancelled"];

/** What a person can stage from here.
 *
 *  `answer_key_change` is deliberately absent: the form above stages one with
 *  the key fix that justifies it, in the same request. Offering it here would
 *  let somebody record a key change as the reason for a job where no key
 *  changed, and the trigger is what the audit trail reads back. */
const TRIGGERS = [
  { value: "band_map_change", label: "A band map was retuned" },
  { value: "engine_fix", label: "A scoring engine fix" },
  { value: "manual", label: "Something else — explained below" },
] as const;

const SUBJECTS = [
  { value: "band_map_version", label: "Band map version" },
  { value: "test_version", label: "Test version" },
  { value: "question_version", label: "Question version" },
  { value: "attempt", label: "One attempt" },
] as const;

type SubjectType = (typeof SUBJECTS)[number]["value"];

export function Regrades() {
  const queries = useQueryClient();
  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });
  const admin = isPlatformAdmin(principal.data ?? null);
  const [questionVersionXid, setQuestionVersionXid] = useState("");

  // "The full history of what this item was ever marked against." Read beside
  // the fix form rather than on a screen of its own, because the question it
  // answers is the one being asked at this moment: what did this key say
  // before, and has somebody already tried the change I am about to make. A
  // key fix supersedes rather than edits, so this list only grows — and the
  // superseded rows are the evidence for every regrade that followed them.
  const keyHistory = useQuery({
    queryKey: ["key-history", questionVersionXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET(
        "/question-versions/{xid}/keys",
        { params: { path: { xid: questionVersionXid } } });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(questionVersionXid),
  });
  const [keyText, setKeyText] = useState("");
  const [keyValue, setKeyValue] = useState<KeyValue>(emptyValue);
  const [slotCount, setSlotCount] = useState(1);
  const [note, setNote] = useState("");
  const [opened, setOpened] = useState<string | null>(null);
  /** The key version the editor was last seeded from, so switching question
   *  loads that question's key exactly once and does not fight the author's
   *  typing on every render. */
  const [seededFrom, setSeededFrom] = useState<string | null>(null);
  /** Which job the dialog is asking about, if any. Was a set of xids that had
   *  had a checkbox ticked — a piece of state that outlived the reading it
   *  claimed to record, since it stayed ticked while the numbers behind it
   *  were replanned. */
  const [asking, setAsking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [trigger, setTrigger] = useState<(typeof TRIGGERS)[number]["value"]>(
    "band_map_change");
  const [subjectType, setSubjectType] = useState<SubjectType>("band_map_version");
  const [subjectXid, setSubjectXid] = useState("");
  const [why, setWhy] = useState("");

  const types = useQuery({
    queryKey: ["question-types"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/question-types");
      if (failure) throw failure;
      return data;
    },
    staleTime: Infinity,
  });

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

  // Platform defaults plus this centre's own. The subject of a band-map regrade
  // is the VERSION, not the map: a map is a name over a series of curves, and
  // the curve is what scored anything.
  const bandMaps = useQuery({
    queryKey: ["band-maps"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/band-maps");
      if (failure) throw failure;
      return data;
    },
  });

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
      // The JSON box wins when it has been used, for the same reason it does on
      // the question form: somebody who opened it and pasted a key meant it.
      // Otherwise the key comes from the widget.
      let key: Record<string, unknown>;
      if (keyText.trim()) {
        try {
          key = JSON.parse(keyText);
        } catch {
          throw new Error("The corrected key is not valid JSON.");
        }
      } else {
        const built = toKey(
          controlFor(chosenType, slotIds(slotCount), []), keyValue);
        if (!built) throw new Error("Fill in at least one answer before saving.");
        key = built as Record<string, unknown>;
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

  const stage = useMutation({
    mutationFn: async () => {
      if (!why.trim()) {
        // The server takes any string, including an empty one. It is the only
        // record of why finished exams were rescored, so an empty reason is
        // refused here rather than written to the log.
        throw new Error("Say why this regrade is being staged.");
      }
      const { data, error: failure } = await api.POST("/regrades", {
        body: {
          trigger,
          subject_type: subjectType,
          subject_xid: subjectXid,
          reason: why.trim(),
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      setSubjectXid("");
      setWhy("");
      // Into the impact report, exactly as the key fix does. A staged job that
      // nobody looks at is a job somebody applies without the numbers.
      if (data?.xid) setOpened(data.xid);
      void queries.invalidateQueries({ queryKey: ["regrades"] });
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
      setAsking(null);
      void queries.invalidateQueries({ queryKey: ["regrades"] });
      void queries.invalidateQueries({ queryKey: ["regrade", opened] });
    },
    // The dialog closes on a refusal too, so the reason — which renders on the
    // page behind it — is not hidden by the thing that caused it.
    onError: (failure) => { setAsking(null); setError(problemText(failure)); },
  });

  const withVersions = (questions.data?.items ?? []).filter((q) => q.current_version?.xid);
  const bandMapVersions = (bandMaps.data ?? []).filter((m) => m.current_version?.xid);
  // Only published versions. A draft has never been sat, so a regrade against
  // one would plan across nothing and report an impact of zero — which reads as
  // "this changes nobody" rather than as "you picked a paper nobody sat".
  const publishedTests = (tests.data?.items ?? [])
    .filter((t) => t.current_published_version_xid);
  /** The definition of the question being corrected, so the editor renders the
   *  widget that type declares rather than a JSON box. Resolved through the
   *  chosen VERSION, since that is what the picker holds. */
  const chosenQuestion = withVersions.find(
    (q) => q.current_version!.xid === questionVersionXid);
  const chosenType = types.data?.find((t) => t.key === chosenQuestion?.type_key);

  // A correction almost always ADDS a spelling to a list that is nearly right,
  // so starting from the key in force is most of the work. Seeded once per
  // question rather than on every render, or it would overwrite the edit.
  const currentKey = keyHistory.data?.find((v) => v.is_current) ?? keyHistory.data?.[0];
  if (currentKey && seededFrom !== currentKey.xid) {
    setSeededFrom(currentKey.xid);
    setKeyValue(fromKey(currentKey.key));
    setSlotCount(slotCountOf(currentKey.key));
    setKeyText("");
  }

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

        {questionVersionXid && keyHistory.data && (
          <div className="report">
            <p className="muted">
              What this item has been marked against
              {keyHistory.data.length > 1 && ", newest first"}:
            </p>
            <ul>
              {keyHistory.data.map((version) => (
                <li key={version.xid}>
                  <span className="num">v{version.version_no}</span>{" "}
                  {version.is_current ? <strong>current</strong> : "superseded"}
                  {" · "}{version.reason ?? "initial"}
                  {version.note ? ` · ${version.note}` : ""}
                  <br />
                  <code>{JSON.stringify(version.key)}</code>
                </li>
              ))}
            </ul>
            {keyHistory.data.length > 1 && (
              <p className="muted">
                This key has been changed before. Check that the correction you
                are about to make is not one somebody already tried and undid.
              </p>
            )}
          </div>
        )}

        {/* The same editor the question form uses. This screen is reached when
            marking is already wrong and students are waiting, which is the worst
            possible moment to be composing `{"slots": …}` by hand — and the key
            being corrected is usually the one that needs a spelling ADDED to a
            list, which is a comma here and a rewrite there. */}
        <AnswerKeyEditor
          def={chosenType}
          value={keyValue}
          onChange={setKeyValue}
          slotCount={slotCount}
          onSlotCount={setSlotCount}
          rawText={keyText}
          onRawText={setKeyText}
          bank={[]}
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

      <h2>Stage a regrade by hand</h2>
      <p className="muted">
        For a change with no key fix behind it — a retuned band map, a scoring
        engine fix. This stages a <strong>dry run</strong> like the one above:
        it works out what would move and rescores nothing until you read the
        impact and apply it.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          stage.mutate();
        }}
      >
        <label htmlFor="r-trigger">Why is this being run</label>
        <select
          id="r-trigger"
          value={trigger}
          onChange={(event) =>
            setTrigger(event.target.value as (typeof TRIGGERS)[number]["value"])
          }
        >
          {TRIGGERS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>

        <label htmlFor="r-subject-type">What it applies to</label>
        <select
          id="r-subject-type"
          value={subjectType}
          onChange={(event) => {
            setSubjectType(event.target.value as SubjectType);
            // The xid belongs to the old kind of thing. Keeping it would send a
            // band map version's xid as a test version's and get a 404 that
            // names neither.
            setSubjectXid("");
          }}
        >
          {SUBJECTS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>

        <label htmlFor="r-subject">Which one</label>
        {subjectType === "band_map_version" && (
          <select
            id="r-subject"
            value={subjectXid}
            onChange={(event) => setSubjectXid(event.target.value)}
            required
          >
            <option value="">— choose a band map —</option>
            {bandMapVersions.map((map) => (
              <option key={map.xid} value={map.current_version!.xid}>
                {map.name} · {map.skill} · v{map.current_version!.version_no}
                {map.is_platform_default ? " · platform default" : ""}
              </option>
            ))}
          </select>
        )}
        {subjectType === "test_version" && (
          <select
            id="r-subject"
            value={subjectXid}
            onChange={(event) => setSubjectXid(event.target.value)}
            required
          >
            <option value="">— choose a test —</option>
            {publishedTests.map((test) => (
              <option key={test.xid} value={test.current_published_version_xid!}>
                {test.title} · {test.skills?.join(", ")}
              </option>
            ))}
          </select>
        )}
        {subjectType === "question_version" && (
          <select
            id="r-subject"
            value={subjectXid}
            onChange={(event) => setSubjectXid(event.target.value)}
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
        )}
        {subjectType === "attempt" && (
          <>
            <input
              id="r-subject"
              value={subjectXid}
              onChange={(event) => setSubjectXid(event.target.value)}
              placeholder="Attempt reference"
              required
            />
            <p className="muted">
              {/* There is no listing of attempts anywhere in the product, so
                  this cannot be a picker. The reference comes from Results,
                  where opening a student is the only handle staff get on a
                  sitting. */}
              Typed in, because attempts are not listed. Open the student under{" "}
              <strong>Results</strong> to find the sitting you mean.
            </p>
          </>
        )}

        <label htmlFor="r-why">Why</label>
        <textarea
          id="r-why"
          rows={2}
          value={why}
          onChange={(event) => setWhy(event.target.value)}
          placeholder="Listening curve retuned after the January calibration."
        />
        <p className="muted">
          Kept on the job. If bands move, this sentence is the answer to why they
          moved.
        </p>

        <button disabled={stage.isPending || !subjectXid}>
          {stage.isPending ? "Staging…" : "Stage a dry run"}
        </button>
      </form>

      <h2>Regrade jobs</h2>
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Trigger</th><th>Status</th><th>Attempts</th><th>Created</th><th /></tr>
          </thead>
          <tbody>
            {jobs.data?.map((row) => (
              <tr key={row.xid}>
                <td>{row.trigger ?? "—"}</td>
                <td>
                  <Status value={row.status} />
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
      </div>

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
              <div className="scroll">
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
              </div>

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
                  {/* The confirmation is the point of the whole flow, not
                      ceremony: this rewrites bands on finished exams, and
                      `bands_changed` is the number a teacher has to be willing
                      to defend to that many students.

                      It was an "I have read these numbers" checkbox, and a
                      checkbox has one weakness a dialog does not: it gates the
                      button without ever restating what it is gating. Ticked at
                      the top of a long impact table, it is a claim about a
                      screen you have since scrolled past. The dialog puts the
                      two numbers in front of the click. */}
                  <button onClick={() => setAsking(job.xid)}
                          disabled={apply.isPending}>
                    {apply.isPending ? "Applying…" : "Apply this regrade"}
                  </button>
                  <Confirm
                    open={asking === job.xid}
                    title="Apply this regrade?"
                    confirmLabel="Apply it"
                    busy={apply.isPending}
                    onCancel={() => setAsking(null)}
                    onConfirm={() => apply.mutate(job.xid)}
                    detail={
                      <>
                        <p>
                          <strong>{job.impact?.attempts_total ?? 0}</strong>{" "}
                          {(job.impact?.attempts_total ?? 0) === 1
                            ? "sat attempt is" : "sat attempts are"} rescored,
                          and{" "}
                          <strong>{job.impact?.bands_changed ?? 0}</strong>{" "}
                          {(job.impact?.bands_changed ?? 0) === 1
                            ? "student is told their band has changed"
                            : "students are told their bands have changed"}.
                        </p>
                        <p className="muted">
                          Their previous score runs are kept, so each student&rsquo;s
                          history still shows what they were originally marked.
                          There is no undo for the notification.
                        </p>
                      </>
                    }
                  />
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
