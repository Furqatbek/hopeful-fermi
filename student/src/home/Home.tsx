/**
 * What a student sees on opening the app: their assigned work, and where they
 * are.
 *
 * `docs/design/0013-student-exam-ui.md` §8 names the endpoints — `GET
 * /assignments` and `GET /me/progress`. Both are fetched together because the
 * screen is useless with either half missing, and neither is large.
 *
 * The layout is the bento from the console's design system: the progress
 * readings are three small compartments and the work is a list of larger ones.
 * The exam runner deliberately does NOT use this language — it imitates the real
 * test client — so the two halves of this app look different on purpose. A
 * student should be able to tell at a glance whether they are in an exam.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api, problemText } from "../api/client";
import {
  attemptsLeft, deadlineText, limitText, ordered, stateOf, type Assignment,
} from "./assignments";

function Band({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <section className="cell" style={{ ["--span" as string]: 4 }}>
      <p className="stat__label">{label}</p>
      <p className="stat__value">{value == null ? "—" : value.toFixed(1)}</p>
    </section>
  );
}

function Work({ assignment }: { assignment: Assignment }) {
  const state = stateOf(assignment);
  const left = attemptsLeft(assignment);
  const limit = limitText(assignment);

  return (
    <section className={`cell work work--${state}`} style={{ ["--span" as string]: 6 }}>
      <div className="work__head">
        <h3>{assignment.test_title ?? "Untitled paper"}</h3>
        {/* Exam and practice are genuinely different products — one hearing of
            the audio versus free replay, a clock that cannot be paused versus
            one that can (§6). The badge is the only warning a student gets. */}
        <span className={`badge badge--${assignment.mode}`}>{assignment.mode}</span>
      </div>

      <p className="work__meta">
        <span>{deadlineText(assignment)}</span>
        {limit && <><span aria-hidden="true"> · </span><span>{limit}</span></>}
        <span aria-hidden="true"> · </span>
        <span>
          {left} of {assignment.max_attempts ?? 1}{" "}
          {(assignment.max_attempts ?? 1) === 1 ? "attempt" : "attempts"} left
        </span>
      </p>

      {state === "startable" ? (
        <Link className="work__start" to={`/exam/${assignment.xid}`}>
          {assignment.mode === "exam" ? "Sit this mock" : "Practise"}
        </Link>
      ) : (
        // Not a disabled button. A control that looks like a control and does
        // nothing invites a student to keep pressing it; a sentence tells them
        // why and what to do instead.
        <p className="work__blocked">
          {state === "upcoming" && "Not open yet. It will appear here when it opens."}
          {state === "closed" && "This closed. Ask your teacher to reopen it if you need another go."}
          {state === "exhausted" && "You have used every attempt on this one."}
        </p>
      )}
    </section>
  );
}

export function Home() {
  const assignments = useQuery({
    queryKey: ["assignments"],
    queryFn: async () => {
      const { data, error } = await api.GET("/assignments", { params: { query: {} } });
      if (error) throw error;
      return data;
    },
  });

  const progress = useQuery({
    queryKey: ["me", "progress"],
    queryFn: async () => {
      const { data, error } = await api.GET("/me/progress");
      if (error) throw error;
      return data;
    },
  });

  // No cast. The generated client's `Assignment` already satisfies the shape
  // `assignments.ts` declares, and eslint's no-unnecessary-type-assertion says
  // so — which is worth keeping true: a cast here would silently survive the
  // contract changing underneath it, and the whole reason the client is
  // generated is that it should not.
  const work = ordered(assignments.data?.items ?? []);

  return (
    <main className="page">
      <h1>Your work</h1>

      {(assignments.isError || progress.isError) && (
        <p className="error">
          {problemText(assignments.error ?? progress.error)}
        </p>
      )}

      <div className="bento">
        <Band label="Latest band" value={progress.data?.latest_band} />
        <Band label="Best band" value={progress.data?.best_band} />
        <section className="cell" style={{ ["--span" as string]: 4 }}>
          <p className="stat__label">Mocks sat</p>
          <p className="stat__value">{progress.data?.attempts ?? "—"}</p>
        </section>
      </div>

      <h2>Assigned to you</h2>

      {assignments.isPending && <p className="muted">Loading…</p>}

      {assignments.data && work.length === 0 && (
        <p className="muted">
          Nothing assigned yet. Your centre puts work here — when they do it will
          show up without you needing to refresh.
        </p>
      )}

      <div className="bento">
        {work.map((a) => <Work key={a.xid} assignment={a} />)}
      </div>
    </main>
  );
}
