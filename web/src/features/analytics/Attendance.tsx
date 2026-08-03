/**
 * Who was set work, who did it, and who did it late.
 *
 * **This is the page a centre puts in front of a parent**, which decides its
 * shape. One row per student, the name first and in full, and the whole of that
 * student's record as a single divided bar — so the answer to "how has my
 * daughter been getting on" is one line, not a column of numbers to add up.
 *
 * **Late and not started are the two states this page exists to tell apart.**
 * They are different conversations: one is a child who is working and
 * disorganized, the other is a child who is not there. They are separated by
 * colour and by texture, because roughly one man in twelve cannot use the
 * colour, and they are counted in their own columns as well as drawn.
 *
 * Late work is still completed work. The projection sets `late` from
 * `submitted_at > closes_at` and `completed` from the attempt's status, so a late
 * submission is both, and the bar shows it inside the finished part rather than
 * beside it. Counting it as not done would tell a parent their child did nothing
 * when they did it on Tuesday.
 *
 * **A grid of students against individual assignments is not possible here.** The
 * endpoint returns counts per student — assigned, started, completed, late — and
 * not which assignment each came from, so there is no per-assignment column to
 * draw and this does not pretend otherwise. `Results` is where one particular
 * mock is read student by student.
 *
 * **A student with no row is not a student with nothing to report.** The counts
 * come from `attendance_facts`, which has a row per (assignment, student), so
 * anybody never targeted by an assignment is absent from the response entirely.
 * On this page absent reads as "not in this class", so the class list is fetched
 * alongside and anyone missing is named underneath.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import {
  attendanceRows,
  orderAttendance,
  type RowOrder,
  unlisted,
} from "./cohort";
import "./cohort.css";

export function Attendance() {
  const [orgXid, setOrgXid] = useState("");
  const [chosen, setChosen] = useState("");
  const [order, setOrder] = useState<RowOrder>("name");

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

  const attendance = useQuery({
    queryKey: ["cohort-attendance", chosen],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/cohorts/{xid}/attendance", {
        params: { path: { xid: chosen } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(chosen),
  });

  // The class list, only so the students the grid has no row for can be named.
  // Both endpoints already require a teaching role at this centre, so this asks
  // for nothing the page was not entitled to.
  const members = useQuery({
    queryKey: ["cohort-members", chosen],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/cohorts/{xid}/members", {
        params: { path: { xid: chosen } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(chosen),
  });

  const raw = attendance.data?.rows ?? [];
  const rows = orderAttendance(attendanceRows(raw), order);
  const missing = members.data ? unlisted(members.data, raw) : [];
  const totals = rows.reduce(
    (running, row) => ({
      assigned: running.assigned + row.counts.assigned,
      completed: running.completed + row.counts.completed,
      late: running.late + row.counts.late,
      notStarted: running.notStarted + row.parts.notStarted,
    }),
    { assigned: 0, completed: 0, late: 0, notStarted: 0 });

  if (orgs.isPending) return <div className="page muted">Loading…</div>;
  if (!org) {
    return (
      <div className="page">
        <h1>Attendance</h1>
        <p className="muted">
          You are not a member of any organization, so there are no classes to
          report on.
        </p>
      </div>
    );
  }

  return (
    <div className="page cohort">
      <h1>Attendance</h1>
      <p className="muted">
        Work this centre set, and what each student did with it.
      </p>
      {orgs.isError && <p className="error">{problemText(orgs.error)}</p>}
      {cohorts.isError && <p className="error">{problemText(cohorts.error)}</p>}

      {centres.length > 1 && (
        <>
          <label htmlFor="at-org">Centre</label>
          <select
            id="at-org"
            value={org.xid}
            onChange={(event) => {
              setOrgXid(event.target.value);
              setChosen("");
            }}
          >
            {centres.map((centre) => (
              <option key={centre.xid} value={centre.xid}>{centre.name}</option>
            ))}
          </select>
        </>
      )}

      <label htmlFor="at-cohort">Class</label>
      <select
        id="at-cohort"
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

      {attendance.isError && <p className="error">{problemText(attendance.error)}</p>}
      {attendance.isPending && chosen && <p className="muted">Loading…</p>}

      {attendance.data && (
        <>
          <p className="muted">
            These counts come from a summary the server rebuilds in the
            background, roughly every fifteen minutes. Work handed in during the
            last few minutes may not be counted yet.
          </p>

          {rows.length === 0 ? (
            <p className="muted">
              No work has been set to this class yet, so there is nothing to
              report. Set a mock under <strong>Assignments</strong> and this fills
              in once the class has it.
            </p>
          ) : (
            <>
              <p>
                <strong>{rows.length}</strong> student
                {rows.length === 1 ? "" : "s"} ·{" "}
                <strong>{totals.completed} of {totals.assigned}</strong> pieces of
                work completed
                {totals.late > 0 && (
                  <span className="muted"> · {totals.late} handed in late</span>
                )}
                {totals.notStarted > 0 && (
                  <span className="muted"> · {totals.notStarted} never started</span>
                )}
              </p>

              <div className="row">
                <label htmlFor="at-order">Order</label>
                <select
                  id="at-order"
                  value={order}
                  onChange={(event) => setOrder(event.target.value as RowOrder)}
                >
                  <option value="name">By name</option>
                  <option value="attention">Most work outstanding first</option>
                </select>
              </div>

              <table className="grid">
                <thead>
                  <tr>
                    <th>Student</th><th className="record">Record</th>
                    <th>Set</th><th>Done</th><th>Late</th><th>Not started</th>
                    <th>Done</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.key}>
                      <td className="who">{row.name}</td>
                      <td className="record">
                        <div
                          className="stack"
                          role="img"
                          aria-label={
                            `${row.parts.onTime} on time, ${row.parts.late} late, `
                            + `${row.parts.unfinished} started and unfinished, `
                            + `${row.parts.notStarted} not started`}
                        >
                          {/* Widths out of `assigned`, so every student's bar is
                              the same length and a short one means a student who
                              was set less work — not one who did less of it. */}
                          <Segment kind="done" count={row.parts.onTime}
                                   of={row.counts.assigned} label="on time" />
                          <Segment kind="late" count={row.parts.late}
                                   of={row.counts.assigned} label="late" />
                          <Segment kind="unfinished" count={row.parts.unfinished}
                                   of={row.counts.assigned} label="started, unfinished" />
                          <Segment kind="none" count={row.parts.notStarted}
                                   of={row.counts.assigned} label="not started" />
                        </div>
                      </td>
                      <td className="num">{row.counts.assigned}</td>
                      <td className="num">{row.counts.completed}</td>
                      <td className="num">
                        {row.counts.late > 0
                          ? <strong>{row.counts.late}</strong>
                          : <span className="muted">0</span>}
                      </td>
                      <td className="num">
                        {row.parts.notStarted > 0
                          ? <strong>{row.parts.notStarted}</strong>
                          : <span className="muted">0</span>}
                      </td>
                      <td className="num">
                        {/* No percentage for a student nobody set work to. 0%
                            reads as their failure and it is not theirs. */}
                        {row.completion === null
                          ? <span className="muted">—</span>
                          : `${row.completion}%`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <p className="key">
                <span><i className="swatch seg done" /> Completed on time</span>
                <span><i className="swatch seg late" /> Completed late</span>
                <span><i className="swatch seg unfinished" /> Started, not finished</span>
                <span><i className="swatch seg none" /> Never started</span>
              </p>
              <p className="muted">
                Late work counts as completed — it was handed in after the closing
                time, not skipped.
              </p>
            </>
          )}

          {members.isError && (
            <p className="muted">
              The class list could not be loaded, so this page cannot say whether
              any student is missing from the table above.
            </p>
          )}
          {missing.length > 0 && (
            <p className="muted">
              In this class and not in the table: {missing.join(", ")}. No work has
              ever been set to {missing.length === 1 ? "them" : "them"}, so there is
              nothing to report — it is not a record of absence.
            </p>
          )}
        </>
      )}
    </div>
  );
}

/**
 * One part of a student's bar, or nothing at all when it is empty.
 *
 * A zero-width flex child still renders its border on some engines, which shows
 * as a hairline of the wrong colour in a record that has none of that kind.
 */
function Segment({ kind, count, of, label }: {
  kind: "done" | "late" | "unfinished" | "none";
  count: number;
  of: number;
  label: string;
}) {
  if (count === 0 || of === 0) return null;
  return (
    <span
      className={`seg ${kind}`}
      style={{ width: `${(count / of) * 100}%` }}
      title={`${count} ${label}`}
    />
  );
}
