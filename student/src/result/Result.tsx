/**
 * The band, after submitting.
 *
 * Scoring is synchronous, but `status` can still be `submitted` rather than
 * `scored` when this loads — the contract says to poll — so this refetches until
 * the score run lands rather than showing a student a blank where their band
 * should be.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { problemText } from "../api/client";
import * as attempt from "../exam/attempt";

type Score = {
  status?: string;
  band?: number | null;
  raw_score?: number;
  max_raw?: number;
  per_section?: Record<string, { raw?: number; band?: number }>;
  late_by_ms?: number | null;
};

export function Result() {
  const { xid } = useParams<{ xid: string }>();

  const score = useQuery({
    queryKey: ["attempt", xid, "result"],
    queryFn: () => attempt.result(xid!),
    enabled: !!xid,
    // Poll while the run is still scoring, then stop. `false` rather than 0
    // matters — a 0 interval is a busy loop.
    refetchInterval: (query) => {
      const held = query.state.data as Score | undefined;
      return held && held.status !== "scored" ? 1500 : false;
    },
  });

  const data = score.data as Score | undefined;

  return (
    <main className="page">
      <h1>Your result</h1>

      {score.isError && <p className="error">{problemText(score.error)}</p>}
      {score.isPending && <p className="muted">Marking…</p>}

      {data && data.status !== "scored" && (
        <p className="muted">Marking — this page will update by itself.</p>
      )}

      {data?.status === "scored" && (
        <>
          <div className="bento">
            <section className="cell" style={{ ["--span" as string]: 5 }}>
              <p className="stat__label">Band</p>
              <p className="stat__value">{data.band == null ? "—" : data.band.toFixed(1)}</p>
            </section>
            <section className="cell" style={{ ["--span" as string]: 7 }}>
              <p className="stat__label">Raw score</p>
              <p className="stat__value">
                {data.raw_score ?? "—"}
                <span className="stat__of"> / {data.max_raw ?? "—"}</span>
              </p>
            </section>
          </div>

          {data.per_section && Object.keys(data.per_section).length > 0 && (
            <>
              <h2>By skill</h2>
              <div className="bento">
                {Object.entries(data.per_section).map(([skill, s]) => (
                  <section key={skill} className="cell" style={{ ["--span" as string]: 4 }}>
                    <p className="stat__label">{skill}</p>
                    <p className="stat__value">{s.band == null ? "—" : s.band.toFixed(1)}</p>
                  </section>
                ))}
              </div>
            </>
          )}

          {/* Recorded, not hidden. A student who ran over should know the server
              saw it and marked them anyway — that is the 30-second grace. */}
          {data.late_by_ms != null && data.late_by_ms > 0 && (
            <p className="muted">
              Submitted {Math.round(data.late_by_ms / 1000)}s after time. It was
              accepted and marked.
            </p>
          )}
        </>
      )}

      <p><Link to="/">Back to your work</Link></p>
    </main>
  );
}
