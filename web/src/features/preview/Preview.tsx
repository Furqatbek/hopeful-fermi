/**
 * Sitting your own draft before a cohort does.
 *
 * `POST /test-versions/{xid}/preview` is an AUTHORING action against an
 * unpublished version, and the only way to catch "this section is unanswerable"
 * before thirty students find it. The publish gate catches structure — a missing
 * key, a blank with no answer, audio shorter than the questions asked about it —
 * and cannot catch a sentence whose blank is in the wrong clause, an option bank
 * with two plausible answers, or a passage that never mentions the thing
 * question 7 asks for. Those need a person reading the paper as a student meets
 * it.
 *
 * **This is not the student app.** It renders the same payload the student
 * client receives, from the same endpoint, with the same server-authoritative
 * clock — but the layout is this console's, and saying so matters: an author who
 * believes they are looking at the real thing will draw conclusions about
 * spacing and typography that do not hold.
 *
 * **A preview attempt counts for nothing.** `mode = 'preview'` is excluded from
 * every statistic and from item exposure, which is why previewing a paper
 * repeatedly does not burn it. It is also not a way to see a paper you may not
 * author: the endpoint requires EDIT on the version.
 *
 * Submitting is part of it. The marking is where a bad key shows itself — the
 * author types the answer they believe is right and finds out whether the key
 * agrees, which is the same question a student will ask later and much cheaper
 * to answer now.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, problemText } from "../../api/client";
import { type Option, QuestionView } from "./QuestionView";

/** `mm:ss` from the SERVER's remaining seconds, ticked locally between polls.
 *  The device clock is never consulted — only the delta the server gave us. */
function clock(seconds: number): string {
  const safe = Math.max(0, seconds);
  return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
}

export function Preview() {
  const { xid = "" } = useParams();
  const queries = useQueryClient();
  const [attempt, setAttempt] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, Record<string, string>>>({});
  const [remaining, setRemaining] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [seq, setSeq] = useState(1);

  const start = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST(
        "/test-versions/{xid}/preview", { params: { path: { xid } } });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      setAttempt(data?.xid ?? null);
      setRemaining(data?.seconds_remaining ?? null);
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const paper = useQuery({
    queryKey: ["preview-paper", attempt],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/attempts/{xid}/payload", {
        params: { path: { xid: attempt! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(attempt),
  });

  const state = useQuery({
    queryKey: ["preview-state", attempt],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/attempts/{xid}", {
        params: { path: { xid: attempt! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(attempt),
    // Re-synced from the server rather than trusted to a local counter: the
    // server is the sole authority on time remaining, and a tab left in the
    // background stops ticking.
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (state.data?.seconds_remaining != null) {
      setRemaining(state.data.seconds_remaining);
    }
  }, [state.data?.seconds_remaining]);

  useEffect(() => {
    if (remaining === null) return;
    const tick = setInterval(() => setRemaining((r) => (r === null ? r : r - 1)), 1000);
    return () => clearInterval(tick);
  }, [remaining === null]);

  const save = useMutation({
    mutationFn: async (delta: { xid: string; slot: string; value: string }) => {
      const { error: failure } = await api.POST("/attempts/{xid}/answers", {
        params: {
          path: { xid: attempt! },
          // Required by the contract, and it means what it says: this endpoint
          // is retried by clients on unreliable networks, and a replay must
          // return the stored response rather than applying a delta twice. One
          // key per batch — reusing it across batches would make the second one
          // a replay of the first and silently drop an answer.
          header: { "Idempotency-Key": crypto.randomUUID() },
        },
        body: {
          deltas: [{
            question_version_xid: delta.xid,
            slot_key: delta.slot,
            // A string, which is what the contract declares a per-slot answer
            // to be. An empty box clears the answer rather than storing "".
            response: delta.value === "" ? null : delta.value,
            client_seq: seq,
          }],
        },
      });
      if (failure) throw failure;
      setSeq((n) => n + 1);
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const submit = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/attempts/{xid}/submit", {
        params: { path: { xid: attempt! } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      void queries.invalidateQueries({ queryKey: ["preview-state", attempt] });
      void queries.invalidateQueries({ queryKey: ["preview-review", attempt] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const submitted = state.data?.status === "submitted" || state.data?.status === "scored";

  const review = useQuery({
    queryKey: ["preview-review", attempt],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/attempts/{xid}/review", {
        params: { path: { xid: attempt! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(attempt) && submitted,
  });

  function answer(questionXid: string, slot: string, value: string) {
    setAnswers((all) => ({ ...all, [questionXid]: { ...all[questionXid], [slot]: value } }));
    save.mutate({ xid: questionXid, slot, value });
  }

  if (!attempt) {
    return (
      <div className="page">
        <h1>Preview</h1>
        <p className="muted">
          Sit this draft as a student would. The publish gate checks structure;
          it cannot tell you that a blank is in the wrong clause, that two
          options are both defensible, or that the passage never mentions what
          question 7 asks for. Reading it as an entrant is the only thing that
          can.
        </p>
        <p className="muted">
          Nothing here counts: a preview attempt is excluded from every statistic
          and from item exposure, so previewing a paper does not burn it.
        </p>
        {error && <p className="error">{error}</p>}
        <button onClick={() => start.mutate()} disabled={start.isPending}>
          {start.isPending ? "Starting…" : "Start a preview"}
        </button>{" "}
        <Link className="link" to={`/versions/${xid}`}>Back to composition</Link>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="row">
        <h1>{paper.data?.title ?? "Preview"}</h1>
        {remaining !== null && !submitted && (
          <span className="num" aria-label="Time remaining">{clock(remaining)}</span>
        )}
      </div>
      <p className="muted">
        Preview attempt · the same payload a student's device receives, laid out
        by this console rather than by the student app.
      </p>
      {error && <p className="error">{error}</p>}

      {paper.data?.sections?.map((section) => (
        <section key={section.position} className="paper">
          <h2>{section.title}</h2>
          <p className="muted">
            {section.skill}
            {section.time_limit_seconds
              ? ` · ${Math.round(section.time_limit_seconds / 60)} minutes`
              : ""}
            {section.audio ? " · listening audio" : ""}
          </p>

          {section.passage && (
            <div className="passage-view">
              <h3>{section.passage.title}</h3>
              {section.passage.blocks?.map((block, index) => (
                <p key={index}>
                  {/* The paragraph letter matching-headings questions refer to.
                      Assigned server-side; if it is missing here, a matching
                      question has nothing to point at. */}
                  {block.label && <span className="para-label">{block.label}</span>}
                  {(block.runs ?? []).map((run, i) => (
                    <span key={i}>{run.v}</span>
                  ))}
                </p>
              ))}
            </div>
          )}

          {section.groups?.map((group, gi) => (
            <div key={gi}>
              <h3>
                Questions from {group.number_start}
              </h3>
              {group.instructions && (
                <p>
                  <strong>
                    {String((group.instructions as Record<string, string>)["en"] ?? "")}
                  </strong>
                </p>
              )}
              {group.word_limit?.max_words && (
                <p className="muted">
                  {/* Enforced by the marker, so it belongs in front of the
                      author exactly as it will be in front of the student. */}
                  No more than {group.word_limit.max_words} word
                  {group.word_limit.max_words === 1 ? "" : "s"}
                  {group.word_limit.allow_number ? " and/or a number" : ""}.
                </p>
              )}
              <ol className="tree">
                {group.questions?.map((question) => (
                  <QuestionView
                    key={question.question_version_xid}
                    question={question}
                    bank={(group.option_bank ?? []) as Option[]}
                    answers={answers[question.question_version_xid!] ?? {}}
                    onAnswer={(slot, value) =>
                      answer(question.question_version_xid!, slot, value)
                    }
                  />
                ))}
              </ol>
            </div>
          ))}
        </section>
      ))}

      {!submitted && (
        <div className="row">
          <button onClick={() => submit.mutate()} disabled={submit.isPending}>
            {submit.isPending ? "Submitting…" : "Submit and see the marking"}
          </button>
          <span className="muted">
            The marking is where a bad key shows itself — type the answer you
            believe is right and find out whether the key agrees.
          </span>
        </div>
      )}

      {submitted && review.data && (
        <div className="issued">
          <h2>Marking · band {review.data.band ?? "—"}</h2>
          <table>
            <thead>
              <tr><th>#</th><th>You typed</th><th>Marked</th><th>Key accepts</th></tr>
            </thead>
            <tbody>
              {review.data.items?.map((item) => (
                <tr key={`${item.question_version_xid}-${item.slot_key}`}>
                  <td className="num">{item.number}</td>
                  <td>
                    {item.raw_response || <span className="muted">blank</span>}
                    {item.normalized_response
                      && item.normalized_response !== item.raw_response && (
                      <span className="muted"> → {item.normalized_response}</span>
                    )}
                  </td>
                  <td className={item.verdict === "incorrect" ? "error" : undefined}>
                    {item.verdict}
                  </td>
                  <td className="muted">{item.accepted_answers?.join(" · ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted">
            An answer you are sure of, marked wrong, is a key to correct under{" "}
            <strong>Keys and regrades</strong> — and correcting it now, before the
            paper is published, costs nothing and moves nobody's band.
          </p>
          <Link className="link" to={`/versions/${xid}`}>Back to composition</Link>
        </div>
      )}
    </div>
  );
}
