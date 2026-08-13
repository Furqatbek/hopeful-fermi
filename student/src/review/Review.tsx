/**
 * What was right, and why.
 *
 * `GET /attempts/{xid}/review` has been fully implemented on the server for
 * months — per slot: the verdict, the points, what the student wrote, what it
 * normalised to, every accepted answer, which alternative matched, the
 * normaliser chain that ran, and for listening the transcript at the moment the
 * answer was spoken. The contract calls it "the single most useful support tool
 * in the product". There was no route, no component and no link, so a student
 * could see a band and never see a single question they got wrong.
 *
 * ── two requests, deliberately ──────────────────────────────────────────────
 *
 * Review returns marking, not questions: one item per SLOT, identified by
 * `(question_version_xid, slot_key)`. The paper comes from the payload
 * endpoint the student already sat. Showing "you wrote 43" without the sentence
 * it belonged to answers nothing, so both are fetched and joined — see
 * `marking.ts`, where the joining lives so it can be tested.
 *
 * ── the refusals are the feature ────────────────────────────────────────────
 *
 * Four different things can legitimately withhold this screen, and each one is a
 * different sentence and a different thing to do next. A student told only
 * "Forbidden" will ask their teacher, who will ask the owner. So each refusal is
 * rendered with what it is waiting for and, where the server says so, when it
 * opens.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, problemText } from "../api/client";
import { QuestionView } from "../exam/Question";
import {
  answersFor, nearMiss, organise, tally,
  type Item, type MarkedQuestion, type Section,
} from "./marking";
import * as attempt from "../exam/attempt";

type Problem = { code?: string; opens_at?: string; ends_at?: string; title?: string };

const VERDICT_LABEL: Record<string, string> = {
  correct: "Correct",
  incorrect: "Wrong",
  partial: "Part marks",
  unanswered: "Left blank",
  void: "Not marked",
};

async function review(xid: string) {
  const { data, error } = await api.GET("/attempts/{xid}/review", {
    params: { path: { xid } },
  });
  if (error) throw error;
  return data;
}

/** A refusal, said in a way that names what happens next. */
function Withheld({ error }: { error: unknown }) {
  const problem = (error ?? {}) as Problem;
  const when = (iso?: string) =>
    iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : null;

  let heading = "You cannot see this yet";
  let body = problemText(error);

  if (problem.code === "review_not_permitted") {
    heading = "Answers are not shown for this paper";
    body = "Your centre has chosen not to release the answers for this one. "
      + "Your teacher can still go through it with you.";
  } else if (problem.code === "review_not_yet_open") {
    heading = "Answers open when the assignment closes";
    const at = when(problem.opens_at);
    body = at
      ? `Come back after ${at}. Classmates are still sitting this paper.`
      : "Come back after the deadline. Classmates are still sitting this paper.";
  } else if (problem.code === "competition_still_live") {
    heading = "Answers open when the contest finishes";
    const at = when(problem.ends_at);
    body = at ? `The contest ends at ${at}.` : problemText(error);
  } else if (problem.code === "not_scored") {
    heading = "This paper has not been marked yet";
    body = "If you have just submitted, give it a moment and reload.";
  }

  return (
    <main className="page">
      <h1>{heading}</h1>
      <p className="muted">{body}</p>
      <p><Link to="/">Back to your work</Link></p>
    </main>
  );
}

function Slot({ item }: { item: Item }) {
  const written = (item.raw_response ?? "").trim();
  const accepted = item.accepted_answers ?? [];
  const hint = nearMiss(item);

  return (
    <li className={`mark mark--${item.verdict}`}>
      <span className="mark__verdict">{VERDICT_LABEL[item.verdict] ?? item.verdict}</span>
      <span className="mark__answers">
        <span className="mark__yours">
          {written
            ? <>You wrote <b>{written}</b></>
            : <span className="muted">You left this blank</span>}
        </span>
        {/* The accepted list is shown even when the answer was right: knowing
            the OTHER things that would have scored is most of the learning. */}
        {accepted.length > 0 && (
          <span className="mark__accepted">
            {accepted.length === 1 ? "Accepted: " : "Accepted any of: "}
            {accepted.join(" · ")}
          </span>
        )}
        {hint && <span className="mark__hint">{hint}</span>}
      </span>
      <span className="mark__points">
        {item.awarded}/{item.max_points}
      </span>
    </li>
  );
}

function Marked({ entry }: { entry: MarkedQuestion }) {
  const heard = entry.items.map((i) => i.transcript_excerpt).find(Boolean);

  return (
    <article className="reviewq">
      <div className="reviewq__head">
        <span className="q__number">{entry.number}</span>
        {/* Only when there is something to add up. On a single-gap question the
            slot below already says 1/1, and saying it twice reads as two
            different facts. */}
        {entry.items.length > 1 && (
          <span className="reviewq__score">{entry.awarded} of {entry.max}</span>
        )}
      </div>

      {entry.question
        ? (
          // The question exactly as it was sat, with the student's own answers
          // filled in and every control disabled. Re-reading the question is
          // most of what review is for, and rebuilding a read-only variant of
          // seventeen types would be a second renderer to keep in step.
          <QuestionView
            question={entry.question}
            group={{ questions: [] }}
            answers={answersFor(entry)}
            onAnswer={() => undefined}
            disabled
          />
        )
        : (
          <p className="muted">
            This question was marked but is not in the paper as published.
          </p>
        )}

      <ul className="marks">
        {entry.items.map((item) => <Slot key={item.slot_key} item={item} />)}
      </ul>

      {/* Listening only: the sentence the answer was in. This is the single most
          useful thing on the screen for a student who cannot hear why they were
          wrong, and it is why the transcript is stored at all. */}
      {heard && (
        <p className="reviewq__heard">
          <span className="reviewq__heard-label">You heard</span> “{heard}”
        </p>
      )}
    </article>
  );
}

export function Review() {
  const { xid } = useParams<{ xid: string }>();

  const marking = useQuery({
    queryKey: ["attempt", xid, "review"],
    queryFn: () => review(xid!),
    enabled: !!xid,
    retry: false,   // every failure here is a decision, not a blip
  });

  const paper = useQuery({
    queryKey: ["attempt", xid, "payload"],
    queryFn: () => attempt.payload(xid!),
    enabled: !!xid && marking.isSuccess,
  });

  if (marking.isPending) {
    return <main className="page"><p className="muted">Reading the marking…</p></main>;
  }
  if (marking.isError) return <Withheld error={marking.error} />;

  const items = (marking.data?.items ?? []) as Item[];
  const sections = ((paper.data?.sections ?? []) as unknown as Section[]);
  const grouped = organise(sections, items);
  const overall = tally(items);
  const band = marking.data?.band;

  return (
    <main className="page">
      <h1>Your answers</h1>

      <div className="bento">
        <section className="cell" style={{ ["--span" as string]: 4 }}>
          <p className="stat__label">Band</p>
          <p className="stat__value">{band == null ? "—" : band.toFixed(1)}</p>
        </section>
        <section className="cell" style={{ ["--span" as string]: 8 }}>
          <p className="stat__label">Right</p>
          <p className="stat__value">
            {overall.right}<span className="stat__of"> of {overall.of}</span>
          </p>
        </section>
      </div>

      {paper.isPending && <p className="muted">Fetching the paper…</p>}
      {paper.isError && (
        // The marking is already here, so show it. Losing the questions makes
        // this screen worse; it does not make it useless.
        <p className="runner__notice">
          The questions could not be loaded, so only the marking is shown below.
        </p>
      )}

      {grouped.map((section) => (
        <section key={section.position} className="reviewsec">
          <h2>
            {section.title}
            <span className="reviewsec__score">{section.awarded} of {section.max}</span>
          </h2>
          {section.questions.map((entry) => <Marked key={entry.xid} entry={entry} />)}
        </section>
      ))}

      {grouped.length === 0 && (
        <p className="muted">There is nothing marked on this attempt.</p>
      )}

      <p><Link to="/">Back to your work</Link></p>
    </main>
  );
}
