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
 *
 * **An item is shared, so where it is used is shown before it is changed.** One
 * question version can sit in several groups and several papers — that reuse is
 * the point of a bank — and `GET /questions/{xid}/usage` is the only way to see
 * that before typing. It is rendered above the payload editor, never behind a
 * second click, because a warning read after the save is not a warning.
 */

import { ArchiveButton } from "../archive/ArchiveButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { editError, useVersionEdit } from "../edit/useVersionEdit";
import { UsagePanel } from "../usage/UsagePanel";
import { AnswerKeyEditor } from "./AnswerKeyEditor";
import { type KeyValue, controlFor, emptyValue, slotIds, toKey } from "./answerKey";
import { type FormField, type Payload, TypeForm } from "./TypeForm";
import { VisibilityPicker } from "../archive/VisibilityPicker";

/** The question being looked at, and whether the payload editor is open.
 *  Both xids are needed and they are different things: usage is asked of the
 *  QUESTION, edits are addressed to the VERSION. */
interface Opened {
  question: string;
  version: string;
  typeKey: string;
  editing: boolean;
}

export function QuestionLibrary() {
  const queries = useQueryClient();
  const [typeKey, setTypeKey] = useState("");
  const [skill, setSkill] = useState<"reading" | "listening">("reading");
  const [payload, setPayload] = useState<Payload>({});
  const [keyText, setKeyText] = useState("");
  const [keyValue, setKeyValue] = useState<KeyValue>(emptyValue);
  const [slotCount, setSlotCount] = useState(1);

  /** The options this question itself carries, for the types whose key is a
   *  choice out of them (`mcq_single`, `mcq_multi`). Typed one per line into
   *  the `option_list` field above, so the picker can offer the real wording
   *  instead of asking the author to remember which letter it was. The bank for
   *  a `group.option_bank` type is NOT here — it lives on the question group,
   *  which this form does not have in front of it, so those fall back to a
   *  typed letter and say so. */
  const bank = Array.isArray((payload as { options?: unknown }).options)
    ? ((payload as { options: unknown[] }).options
        .filter((o): o is string => typeof o === "string"))
    : [];
  const [error, setError] = useState<string | null>(null);
  const [opened, setOpened] = useState<Opened | null>(null);
  const [editPayload, setEditPayload] = useState<Payload>({});

  const edit = useVersionEdit("/question-versions/{xid}", ["questions"]);

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

  // The EDITED question's own type, not the one selected in the create form
  // above. They are unrelated: a question's type is fixed at creation and
  // `QuestionVersionUpdate` cannot change it, so driving the edit form from
  // `chosen` rendered the fields of whatever type happened to be picked for the
  // next new question — usually none at all, which is an edit form with no
  // fields that saves the payload back unchanged.
  const editedType = types.data?.find((type) => type.key === opened?.typeKey);
  const editFields =
    ((editedType?.authoring as { form?: FormField[] } | undefined)?.form ?? []);

  const create = useMutation({
    mutationFn: async () => {
      // The typed JSON wins when it is there, because that box is the escape
      // hatch — somebody who opened it and pasted a key meant it. Otherwise the
      // key is built from the widget, which is what everybody else uses.
      let key: Record<string, unknown> | undefined;
      if (keyText.trim()) {
        try {
          key = JSON.parse(keyText);
        } catch {
          throw new Error("The answer key is not valid JSON.");
        }
      } else {
        const built = toKey(controlFor(chosen as never, slotIds(slotCount), bank), keyValue);
        if (built) key = built as Record<string, unknown>;
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
            // The key is shaped by the type too: a TRUE/FALSE answer carried
            // into a gap-fill is a key for a question that no longer exists.
            setKeyValue(emptyValue());
            setKeyText("");
            setSlotCount(1);
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

        <AnswerKeyEditor
          def={chosen}
          value={keyValue}
          onChange={setKeyValue}
          slotCount={slotCount}
          onSlotCount={setSlotCount}
          rawText={keyText}
          onRawText={setKeyText}
          bank={bank}
        />

        <button disabled={create.isPending || !typeKey}>
          {create.isPending ? "Saving…" : "Create question"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}
      {types.isError && <p className="error">{problemText(types.error)}</p>}

      <table>
        <thead>
          <tr><th>Type</th><th>Skill</th><th>Version</th><th>Slots</th><th>Visible to</th><th /><th /><th /></tr>
        </thead>
        <tbody>
          {questions.data?.items?.map((question) => {
            const current = question.current_version;
            return (
              <tr key={question.xid}>
                <td>{question.type_key}</td>
                <td>{question.skill}</td>
                <td className="muted">
                  v{current?.version_no} · {current?.status}
                </td>
                <td className="muted">
                  {/* Slot keys, extracted SERVER-SIDE from the payload — the
                      publish gate compares the answer key against exactly this
                      array, which is why a client that declared its own would be
                      validating its own claim.
                      Deliberately not a "has a key?" column: the contract exposes
                      no such field, and inventing one from slot count would be a
                      guess shown as a fact. */}
                  {current?.slot_keys?.length ?? 0}
                </td>
                <td>
                  {/* Offered for every item, published or not. "Which papers is
                      this in" is a question about a published item too — it is how
                      an author decides whether a key fix is worth a regrade — and
                      it changes nothing, so there is no reason to gate it. */}
                  <button
                    className="link"
                    onClick={() => {
                      setError(null);
                      setOpened(
                        opened?.question === question.xid && !opened.editing
                          ? null
                          : {
                              question: question.xid,
                              version: current?.xid ?? "",
                              typeKey: question.type_key,
                              editing: false,
                            },
                      );
                    }}
                  >
                    {opened?.question === question.xid && !opened.editing
                      ? "Hide"
                      : "Where used"}
                  </button>
                </td>
                <td>
                  {/* Only a DRAFT. A published question version is frozen — an
                      attempt scored against it has to keep meaning what it meant —
                      and offering the control would be a button the server
                      refuses with `version_immutable`. */}
                  {current?.status === "draft" && (
                    <button
                      className="link"
                      onClick={() => {
                        setError(null);
                        setOpened(
                          opened?.question === question.xid && opened.editing
                            ? null
                            : {
                                question: question.xid,
                                version: current.xid,
                                typeKey: question.type_key,
                                editing: true,
                              },
                        );
                        setEditPayload((current.payload ?? {}));
                      }}
                    >
                      {opened?.question === question.xid && opened.editing
                        ? "Close"
                        : "Edit"}
                    </button>
                  )}
                </td>
                <td>
                  <VisibilityPicker endpoint="/questions/{xid}/visibility"
                                    xid={question.xid ?? ""}
                                    visibility={question.visibility}
                                    invalidate={["questions"]} />
                </td>
                <td>
                  <ArchiveButton endpoint="/questions/{xid}/archive"
                                 xid={question.xid ?? ""}
                                 invalidate={["questions"]} label="question" />
                </td>
              </tr>
            );
          })}
          {questions.data?.items?.length === 0 && (
            <tr><td colSpan={8} className="muted">No questions yet.</td></tr>
          )}
        </tbody>
      </table>

      {/* Not `issued`: the usage panel below draws that box itself, and nesting
          two accented boxes reads as two warnings when there is one. */}
      {opened && (
        <div>
          <h2>{opened.editing ? "Edit question" : "Where this item is used"}</h2>

          {/* First, and in both modes. An author who opened this to edit reads
              the blast radius before the form, not after the save. */}
          <UsagePanel subject="question" xid={opened.question} />

          {opened.editing && (
            <>
              <p className="muted">
                The payload only. Slot keys are re-extracted server-side from the
                text, so moving a blank moves what the key has to line up with — the
                publish gate will say so if they stop matching.
              </p>
              <TypeForm fields={editFields} value={editPayload} onChange={setEditPayload} />
              <div className="row">
                <button
                  disabled={edit.isPending || !opened.version}
                  onClick={() =>
                    edit.mutate(
                      { xid: opened.version, body: { payload: editPayload } },
                      {
                        onSuccess: () => setOpened(null),
                        onError: (failure) => setError(editError(failure)),
                      },
                    )
                  }
                >
                  {edit.isPending ? "Saving…" : "Save"}
                </button>
                <button type="button" className="link" onClick={() => setOpened(null)}>
                  Cancel
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
