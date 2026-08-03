/**
 * The question bank, and a form the REGISTRY drives.
 *
 * Choosing a type fetches its definition and renders `authoring.form`. Nothing
 * here knows what a `matching_headings` is — which is the point, because a type
 * registered tomorrow gets a form tomorrow.
 *
 * The answer key is entered alongside the question and sent in the same request.
 * `QuestionCreate.key` is optional in the contract and the temptation is to make
 * it a later step; it should not be. A bank of questions with no keys is a bank
 * that cannot be scored, and "bad keys are the fastest way to lose a school
 * client" — the moment to write one is while the question is in front of you.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { type FormField, type Payload, TypeForm } from "./TypeForm";

export function QuestionLibrary() {
  const queries = useQueryClient();
  const [typeKey, setTypeKey] = useState("");
  const [skill, setSkill] = useState<"reading" | "listening">("reading");
  const [payload, setPayload] = useState<Payload>({});
  const [keyText, setKeyText] = useState("");
  const [error, setError] = useState<string | null>(null);

  const types = useQuery({
    queryKey: ["question-types"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/question-types");
      if (failure) throw failure;
      return data;
    },
  });

  const questions = useQuery({
    queryKey: ["questions"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/questions", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  // A bare array, like `/tests/{xid}/versions`. The contract has 19 bare-array
  // list endpoints and 9 paged ones — bounded lists are bare, unbounded are
  // paged — so the shape is per-endpoint and worth reading rather than assuming.
  const chosen = types.data?.find((type) => type.key === typeKey);
  const fields = ((chosen?.authoring as { form?: FormField[] } | undefined)?.form ?? []);

  const create = useMutation({
    mutationFn: async () => {
      let key: Record<string, unknown> | undefined;
      if (keyText.trim()) {
        try {
          key = JSON.parse(keyText);
        } catch {
          throw new Error("The answer key is not valid JSON.");
        }
      }
      const { error: failure } = await api.POST("/questions", {
        body: {
          type_key: typeKey,
          // Required, and taken from the definition rather than defaulted to 1:
          // a type is `key@version` and the registry keeps old versions live so
          // published tests keep scoring the way they were published. Guessing
          // the version would silently author against the wrong one.
          type_version: chosen?.version ?? 1,
          skill,
          points: 1,
          payload,
          ...(key
            ? {
                key: {
                  key,
                  // `initial` because this is the first key this question has
                  // ever had. The other reasons — `key_fix`, `clarification`,
                  // `import` — belong to LATER keys, and the distinction is not
                  // bookkeeping: a `key_fix` on a published test is what the
                  // regrade flow keys off, and mislabelling the first one as a
                  // fix would suggest a correction to attempts that never
                  // existed.
                  reason: "initial" as const,
                },
              }
            : {}),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setPayload({});
      setKeyText("");
      setError(null);
      void queries.invalidateQueries({ queryKey: ["questions"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  return (
    <div className="page">
      <h1>Questions</h1>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          create.mutate();
        }}
      >
        <label htmlFor="type">Question type</label>
        <select
          id="type"
          value={typeKey}
          onChange={(event) => {
            setTypeKey(event.target.value);
            // Payload is shaped by the TYPE. Carrying fields across a type
            // change would send a `short_answer` body under a `matching_headings`
            // key and produce a validation error about a field the author never
            // filled in.
            setPayload({});
          }}
          required
        >
          <option value="">— choose a type —</option>
          {types.data?.map((type) => (
            <option key={`${type.key}@${type.version}`} value={type.key}>
              {type.title} ({type.key})
            </option>
          ))}
        </select>
        {chosen?.description && <p className="muted">{chosen.description}</p>}

        <label htmlFor="q-skill">Skill</label>
        <select
          id="q-skill"
          value={skill}
          onChange={(event) => setSkill(event.target.value as "reading" | "listening")}
        >
          <option value="reading">Reading</option>
          <option value="listening">Listening</option>
        </select>

        {chosen && fields.length === 0 && (
          <p className="muted">
            This type declares no per-question fields — everything it needs lives
            on the GROUP, so create it from a question group instead.
          </p>
        )}

        <TypeForm fields={fields} value={payload} onChange={setPayload} />

        <label htmlFor="key">Answer key (JSON)</label>
        <p className="muted">
          {/* Shaped by the type's `key_schema`. Text types take a list of
              accepted alternatives per slot — the alternatives are the whole
              point: "fourteen" and "14" are both right, and a key that only
              accepts one marks a correct answer wrong. */}
          Shaped by this type's <code>key_schema</code>. For text answers each slot
          carries <code>accept</code> — every alternative that should be marked
          correct.
        </p>
        <textarea
          id="key"
          rows={4}
          value={keyText}
          onChange={(event) => setKeyText(event.target.value)}
          placeholder={'{"slots": {"s1": {"accept": ["fourteen", "14"], "case_sensitive": false}}}'}
        />

        <button disabled={create.isPending || !typeKey}>
          {create.isPending ? "Saving…" : "Create question"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}
      {types.isError && <p className="error">{problemText(types.error)}</p>}

      <table>
        <thead>
          <tr><th>Type</th><th>Skill</th><th>Version</th><th>Slots</th></tr>
        </thead>
        <tbody>
          {questions.data?.items?.map((question) => (
            <tr key={question.xid}>
              <td>{question.type_key}</td>
              <td>{question.skill}</td>
              <td className="muted">
                v{question.current_version?.version_no} · {question.current_version?.status}
              </td>
              <td className="muted">
                {/* Slot keys, extracted SERVER-SIDE from the payload — the
                    publish gate compares the answer key against exactly this
                    array, which is why a client that declared its own would be
                    validating its own claim.
                    Deliberately not a "has a key?" column: the contract exposes
                    no such field, and inventing one from slot count would be a
                    guess shown as a fact. */}
                {question.current_version?.slot_keys?.length ?? 0}
              </td>
            </tr>
          ))}
          {questions.data?.items?.length === 0 && (
            <tr><td colSpan={4} className="muted">No questions yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
