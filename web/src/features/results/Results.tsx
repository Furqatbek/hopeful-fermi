/**
 * How a class did on a mock, and why each answer was marked the way it was.
 *
 * This reads `/assignments/{xid}/progress` — the same endpoint the invigilation
 * panel polls, asked after the fact rather than during. There is no listing of
 * attempts anywhere in the product, so the progress row's `attempt_xid` is the
 * only handle a member of staff ever gets on a sitting, and opening a student
 * here is what makes `/attempts/{xid}/review` reachable at all.
 *
 * **Reading is not invigilating.** Staff may read a student's result and their
 * marking; they may not fetch the live paper, type an answer, or submit. The
 * server enforces that split — those routes still refuse anyone who did not sit
 * the attempt — and this screen only ever reads.
 *
 * Only work this centre SET. A self-serve practice attempt carries no assignment
 * and never appears here, which is the same line the server draws.
 *
 * The distribution is the number worth showing. A mean band hides the shape: a
 * class averaging 6.0 because everyone scored 6.0 and a class averaging 6.0
 * because half scored 4.5 and half 7.5 need completely different lessons, and the
 * second is invisible in the average.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { distribution, mean } from "./distribution";

export function Results() {
  const [chosen, setChosen] = useState("");
  const [opened, setOpened] = useState<string | null>(null);

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

  const progress = useQuery({
    queryKey: ["assignment-progress", chosen],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/assignments/{xid}/progress", {
        params: { path: { xid: chosen } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(chosen),
  });

  const review = useQuery({
    queryKey: ["review", opened],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/attempts/{xid}/review", {
        params: { path: { xid: opened! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(opened),
    // Every fetch is recorded in the audit log as a staff member opening a
    // student's paper. Refetching on a window focus would write rows nobody
    // performed, and a log that records reads that did not happen is worse than
    // no log — so this is fetched when asked for and not again.
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const students = progress.data?.students ?? [];
  const scored = students.filter((s) => typeof s.band === "number");
  const bands = scored.map((s) => s.band as number);
  const average = mean(bands);
  const histogram = distribution(bands);
  const tallest = Math.max(1, ...histogram.map((h) => h.count));

  return (
    <div className="page">
      <h1>Results</h1>

      <label htmlFor="res-a">Assignment</label>
      <select id="res-a" value={chosen} onChange={(event) => setChosen(event.target.value)}>
        <option value="">— choose an assignment —</option>
        {assignments.data?.items?.map((assignment) => (
          <option key={assignment.xid} value={assignment.xid}>
            {assignment.test_title}
            {assignment.cohort?.name ? ` · ${assignment.cohort.name}` : ""} ·{" "}
            {new Date(assignment.closes_at).toLocaleDateString()}
          </option>
        ))}
      </select>
      {assignments.data?.items?.length === 0 && (
        <p className="muted">Nothing has been assigned yet.</p>
      )}
      {progress.isError && <p className="error">{problemText(progress.error)}</p>}

      {progress.data && (
        <>
          <h2>Sitting</h2>
          <p className="muted">
            {progress.data.summary?.assigned ?? 0} assigned ·{" "}
            {progress.data.summary?.not_started ?? 0} not started ·{" "}
            {progress.data.summary?.in_progress ?? 0} in progress ·{" "}
            {progress.data.summary?.submitted ?? 0} submitted
          </p>

          <h2>Band distribution</h2>
          {scored.length === 0 ? (
            <p className="muted">
              Nothing scored yet. Marking is synchronous on submit, so this fills in
              as students finish.
            </p>
          ) : (
            <>
              <table className="histogram">
                <tbody>
                  {histogram.map((bucket) => (
                    <tr key={bucket.band}>
                      <td className="band">{bucket.band.toFixed(1)}</td>
                      <td>
                        <span
                          className="bar"
                          style={{ width: `${(bucket.count / tallest) * 100}%` }}
                        />
                      </td>
                      <td className="muted">{bucket.count || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">
                Mean {average} across {scored.length} scored
                {scored.length < students.length && (
                  <> · {students.length - scored.length} not yet scored</>
                )}
                . The mean is the least useful number here — read the shape.
              </p>
            </>
          )}

          <h2>Students</h2>
          <table>
            <thead>
              <tr><th>Name</th><th>Band</th><th>Answered</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {[...students]
                // Scored first and highest band first; unscored to the bottom
                // rather than sorted as zero, which would read as a failed exam.
                .sort((a, b) => (b.band ?? -1) - (a.band ?? -1))
                .map((row) => (
                  <tr key={row.user?.xid}>
                    <td>{row.user?.given_name} {row.user?.family_name}</td>
                    <td>
                      {typeof row.band === "number"
                        ? <strong>{row.band.toFixed(1)}</strong>
                        : <span className="muted">—</span>}
                    </td>
                    <td className="muted">{row.answered}/{row.total}</td>
                    <td className="muted">{row.status}</td>
                    <td>
                      {/* Only once there is something to read. A student who has
                          not started has no attempt, and an unscored one has no
                          marking — offering the control anyway would produce a
                          `not_scored` refusal and an audit row for a paper that
                          was never opened. */}
                      {row.attempt_xid && row.status === "scored" && (
                        <button
                          className="link"
                          onClick={() =>
                            setOpened(opened === row.attempt_xid ? null : row.attempt_xid!)
                          }
                        >
                          {opened === row.attempt_xid ? "Close" : "Marking"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              {students.length === 0 && (
                <tr><td colSpan={5} className="muted">Nobody was targeted.</td></tr>
              )}
            </tbody>
          </table>

          {opened && (
            <div className="issued">
              <h2>Marking</h2>
              {review.isPending && <p className="muted">Opening…</p>}
              {review.isError && (
                <>
                  <p className="error">{problemText(review.error)}</p>
                  <p className="muted">
                    A refusal here is the contest clock, not a permission problem:
                    review on a paper with a live competition opens when the contest
                    ends, for staff as well as students — this response carries the
                    answer key, and a contest can be public and cross-centre.
                  </p>
                </>
              )}
              {review.data && (
                <>
                  <table>
                    <thead>
                      <tr>
                        <th>#</th><th>Their answer</th><th>Marked</th>
                        <th>Accepted</th><th>Why</th>
                      </tr>
                    </thead>
                    <tbody>
                      {review.data.items?.map((item) => (
                        <tr key={`${item.question_version_xid}-${item.slot_key}`}>
                          <td className="num">{item.number}</td>
                          <td>
                            {item.raw_response || <span className="muted">blank</span>}
                            {/* What the marker actually compared, which is the
                                answer to most "but I wrote that" disputes: the
                                normalizers strip case, spacing and articles, and
                                the normalized form is what met the key. */}
                            {item.normalized_response &&
                              item.normalized_response !== item.raw_response && (
                                <span className="muted"> → {item.normalized_response}</span>
                              )}
                          </td>
                          <td>
                            {item.verdict}
                            <span className="muted"> {item.awarded}/{item.max_points}</span>
                          </td>
                          <td className="muted">
                            {item.accepted_answers?.join(" · ")}
                            {item.matched_alternative && (
                              <> (matched <strong>{item.matched_alternative}</strong>)</>
                            )}
                          </td>
                          <td className="muted">
                            {item.audio_range && (
                              <>
                                {Math.floor((item.audio_range.start_ms ?? 0) / 1000)}s–
                                {Math.floor((item.audio_range.end_ms ?? 0) / 1000)}s{" "}
                              </>
                            )}
                            {item.transcript_excerpt}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="muted">
                    Opening this was recorded in the audit log against your name.
                    If the marking is wrong because the KEY is wrong, do not
                    explain it one student at a time — correct it under{" "}
                    <strong>Keys and regrades</strong>, which fixes it for everyone
                    who sat it and moves their bands with it.
                  </p>
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
