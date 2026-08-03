/**
 * How a class did on a mock.
 *
 * This reads `/assignments/{xid}/progress` — the same endpoint the invigilation
 * panel polls, asked after the fact rather than during. That is not a shortcut,
 * it is the only door: **there is no listing of attempts, and `/attempts/{xid}`
 * and its `/result` and `/review` are owner-only.** `_attempt()` refuses any
 * attempt the caller did not sit, with a 404 rather than a 403, and it makes no
 * exception for the teacher who set the work or for a platform admin.
 *
 * So per-item marking — "why was this marked wrong", which the contract itself
 * calls the single most useful support tool in the product — is reachable by the
 * student and by nobody else. This screen does not pretend otherwise and says so
 * where a teacher would go looking for it, because the alternative is a member of
 * staff clicking a student's name and finding nothing.
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
              <tr><th>Name</th><th>Band</th><th>Answered</th><th>Status</th></tr>
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
                  </tr>
                ))}
              {students.length === 0 && (
                <tr><td colSpan={4} className="muted">Nobody was targeted.</td></tr>
              )}
            </tbody>
          </table>

          <p className="muted">
            Per-question marking is not available to staff. An attempt's review —
            what each answer was normalized to and which accepted alternative it
            was compared against — is readable by the student who sat it and by
            nobody else, so a student asking "why was this wrong" has to open it on
            their own device. If a key turns out to be wrong, correct it under{" "}
            <strong>Keys and regrades</strong>; that fixes it for everyone at once
            rather than one conversation at a time.
          </p>
        </>
      )}
    </div>
  );
}
