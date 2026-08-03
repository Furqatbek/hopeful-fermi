/**
 * How a class's bands have moved, week by week.
 *
 * **What is in it is what the centre set.** The view behind this excludes author
 * previews and any attempt with no organization context — a student's own
 * practice at 1 a.m. on their own account is theirs, and it does not enter their
 * school's dashboard. That is the same line the server draws everywhere else in
 * the product and it is a contractual promise, not a preference.
 *
 * **The numbers are a stored summary, not a live query.** `mv_cohort_progress` is
 * a materialized view refreshed by the periodic analytics job, which the
 * scheduler asks for every fifteen minutes. A mock sat this morning is here; one
 * sat ten minutes ago may not be. The screen says so rather than presenting a
 * stale figure as current, because "the system is wrong" and "the system is
 * fifteen minutes behind" are different conversations with a centre.
 *
 * **The per-student columns are NOT a before and after.** The endpoint's
 * `first_band` is `min(avg_band)` over every week and `latest_band` is
 * `max(best_band)`, so they are that student's worst and best weeks in whatever
 * order those fell — and its `delta` is the gap between them, which cannot be
 * negative. A student who went 7.0 in July and 5.0 in August is reported by the
 * API as `first 5.0, latest 7.0, delta +2.0`. Rendering that as improvement would
 * tell a parent their child gained two bands in the term they lost two, so the
 * columns are labelled lowest, highest and spread, which is what they are. The
 * only trustworthy direction on this page is the COHORT trend under each chart,
 * which is read off the weekly series and can go down.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import {
  BAND_CEILING,
  BAND_FLOOR,
  band,
  bandHeight,
  SKILLS,
  studentSpreads,
  trend,
  weekLabel,
  weekSeries,
} from "./cohort";
import "./cohort.css";

const SKILL_LABEL: Record<(typeof SKILLS)[number], string> = {
  overall: "Overall",
  reading: "Reading",
  listening: "Listening",
};

export function CohortProgress() {
  const [orgXid, setOrgXid] = useState("");
  const [chosen, setChosen] = useState("");

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

  const centres = orgs.data?.items ?? [];
  // Falling back to the first rather than requiring a choice: a centre admin
  // belongs to exactly one, and making them pick it every visit is a step that
  // teaches nothing. A platform admin sees every centre and gets the selector.
  const org = centres.find((candidate) => candidate.xid === orgXid) ?? centres[0];
  const activeOrg = org?.xid;

  const cohorts = useQuery({
    queryKey: ["cohorts", activeOrg],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs/{xid}/cohorts", {
        params: { path: { xid: activeOrg! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(activeOrg),
  });

  const progress = useQuery({
    queryKey: ["cohort-progress", chosen],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/cohorts/{xid}/progress", {
        params: { path: { xid: chosen } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(chosen),
  });

  const series = weekSeries(progress.data?.weeks ?? []);
  const spreads = studentSpreads(progress.data?.students ?? []);
  const opens = series[0];
  const closes = series[series.length - 1];
  const sittings = series.reduce((total, point) => total + point.attempts, 0);

  if (orgs.isPending) return <div className="page muted">Loading…</div>;
  if (!org) {
    return (
      <div className="page">
        <h1>Cohort progress</h1>
        <p className="muted">
          You are not a member of any organization, so there are no classes to
          report on.
        </p>
      </div>
    );
  }

  return (
    <div className="page cohort">
      <h1>Cohort progress</h1>
      <p className="muted">
        Bands for the work this centre set. A student's own practice on their own
        account is not counted here, and neither are author previews.
      </p>
      {orgs.isError && <p className="error">{problemText(orgs.error)}</p>}
      {cohorts.isError && <p className="error">{problemText(cohorts.error)}</p>}

      {centres.length > 1 && (
        <>
          <label htmlFor="cp-org">Centre</label>
          <select
            id="cp-org"
            value={org.xid}
            onChange={(event) => {
              setOrgXid(event.target.value);
              // The chosen class belongs to the old centre. Keeping it would ask
              // for a cohort this centre does not have, which answers 404.
              setChosen("");
            }}
          >
            {centres.map((centre) => (
              <option key={centre.xid} value={centre.xid}>{centre.name}</option>
            ))}
          </select>
        </>
      )}

      <label htmlFor="cp-cohort">Class</label>
      <select
        id="cp-cohort"
        value={chosen}
        onChange={(event) => setChosen(event.target.value)}
      >
        <option value="">— choose a class —</option>
        {cohorts.data?.map((cohort) => (
          <option key={cohort.xid} value={cohort.xid}>
            {cohort.name}
            {cohort.academic_year ? ` · ${cohort.academic_year}` : ""} ·{" "}
            {cohort.member_count ?? 0} student{cohort.member_count === 1 ? "" : "s"}
          </option>
        ))}
      </select>
      {cohorts.data?.length === 0 && (
        <p className="muted">
          This centre has no classes yet. Add one under <strong>Centre</strong>.
        </p>
      )}

      {progress.isError && <p className="error">{problemText(progress.error)}</p>}
      {progress.isPending && chosen && <p className="muted">Loading…</p>}

      {progress.data && (
        <>
          <p className="muted">
            These figures come from a summary the server rebuilds in the
            background, roughly every fifteen minutes. A mock sat in the last few
            minutes may not be here yet.
          </p>

          {series.length === 0 ? (
            <p className="muted">
              Nobody in this class has a scored sitting yet. Work set to a class
              appears here once it has been sat and marked.
            </p>
          ) : (
            <>
              <h2>Week by week</h2>
              <div className="smalls">
                {SKILLS.map((skill) => {
                  const direction = trend(series, skill);
                  return (
                    <figure className="small" key={skill}>
                      <figcaption>{SKILL_LABEL[skill]}</figcaption>
                      <div
                        className="cols"
                        role="img"
                        aria-label={series
                          .map((point) =>
                            `${weekLabel(point.week)} ${band(point[skill])}`)
                          .join(", ")}
                      >
                        {series.map((point) => {
                          const height = bandHeight(point[skill]);
                          return (
                            <div
                              className="col"
                              key={point.week}
                              title={`${weekLabel(point.week)}: band ${band(point[skill])}`}
                            >
                              {height === null
                                /* Hatched, not empty. A week with no band for
                                   this skill is a week the class sat nothing of
                                   it, and a blank column would read as a zero. */
                                ? <span className="gap" />
                                : <span className="fill" style={{ height: `${height}%` }} />}
                            </div>
                          );
                        })}
                      </div>
                      <div className="axis">
                        <span>{opens ? weekLabel(opens.week) : ""}</span>
                        <span>{closes ? weekLabel(closes.week) : ""}</span>
                      </div>
                      <p className="muted">
                        {direction ? (
                          <>
                            {band(direction.from)} → <strong>{band(direction.to)}</strong>
                            {" "}({direction.delta > 0 ? "+" : ""}
                            {direction.delta.toFixed(1)})
                          </>
                        ) : (
                          "One week only — not a direction yet."
                        )}
                      </p>
                    </figure>
                  );
                })}
              </div>
              <p className="muted">
                Bars run from band {BAND_FLOOR.toFixed(1)} to band{" "}
                {BAND_CEILING.toFixed(1)}. The figure under each chart is the
                first week compared with the last, so it falls when the class
                does.
              </p>

              <h2>Weeks</h2>
              <table>
                <thead>
                  <tr>
                    <th>Week of</th><th>Sittings</th><th>Overall</th>
                    <th>Reading</th><th>Listening</th>
                  </tr>
                </thead>
                <tbody>
                  {series.map((point) => (
                    <tr key={point.week}>
                      <td>{weekLabel(point.week)}</td>
                      <td className="num">{point.attempts}</td>
                      <td><strong>{band(point.overall)}</strong></td>
                      <td className="num">{band(point.reading)}</td>
                      <td className="num">{band(point.listening)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">
                {sittings} scored sitting{sittings === 1 ? "" : "s"} across{" "}
                {series.length} week{series.length === 1 ? "" : "s"}.
              </p>
            </>
          )}

          <h2>Students</h2>
          <table>
            <thead>
              <tr>
                <th>Name</th><th>Weeks</th><th>Lowest</th><th>Highest</th>
                <th>Spread</th>
              </tr>
            </thead>
            <tbody>
              {spreads.map((student) => (
                <tr key={student.key}>
                  <td>{student.name}</td>
                  <td className="num">{student.weeks}</td>
                  <td className="num">{band(student.lowest)}</td>
                  <td><strong>{band(student.highest)}</strong></td>
                  <td className="num">
                    {student.spread === null ? "—" : student.spread.toFixed(1)}
                  </td>
                </tr>
              ))}
              {spreads.length === 0 && (
                <tr><td colSpan={5} className="muted">Nobody has sat anything yet.</td></tr>
              )}
            </tbody>
          </table>
          <p className="muted">
            Lowest and highest are that student's worst and best weeks, in no
            particular order — the response does not carry each student's weeks
            separately, so this table cannot say whether they improved. Read the
            charts above for the class's direction, and{" "}
            <strong>Results</strong> for one mock in detail. Weeks counts the
            weeks a student sat something, not the number of sittings.
          </p>
        </>
      )}
    </div>
  );
}
