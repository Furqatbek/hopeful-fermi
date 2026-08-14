/**
 * The answer key, edited the way the registry always said it should be.
 *
 * Each question type declares `authoring.key_widget` — ten of them across the
 * seventeen that ship. The console read none, and showed one raw JSON textarea
 * for every type, so setting the answer to a gap-fill meant typing
 * `{"slots": {"s1": {"accept": ["fourteen", "14"]}}}` by hand. A centre admin
 * with a class starting in ten minutes does not do that, and the contract never
 * asked them to: `QuestionTypeDef.authoring` exists, is served, and says
 * "drives the authoring UI's form renderer".
 *
 * Built on `controlFor`, so the branch is on the DECLARED widget rather than on
 * `type_key`. A type registered through `POST /admin/question-types` on a
 * running system gets the right editor with no redeploy — and one whose widget
 * nobody has written yet falls through to the JSON box, named, rather than
 * becoming unauthorable. That fallback is the same load-bearing choice
 * `TypeForm` makes for payload fields.
 *
 * The JSON box stays reachable for every type, folded away. It is how somebody
 * pastes a key from elsewhere, and how an author works around a widget that is
 * wrong about their question — removing the escape hatch would trade one group
 * of stuck people for another.
 */

import { useState } from "react";

import {
  type Control, type KeyValue, LETTERS, controlFor, joinAlternatives, slotIds,
} from "./answerKey";

export function AnswerKeyEditor({ def, value, onChange, slotCount, onSlotCount,
                                  rawText, onRawText, bank, bankLabels }: {
  def: unknown;
  value: KeyValue;
  onChange: (next: KeyValue) => void;
  slotCount: number;
  onSlotCount: (next: number) => void;
  rawText: string;
  onRawText: (next: string) => void;
  bank: string[];
  /** `id -> the words`, so a picker can offer "A — Living near water" while
   *  storing "A". The key names an option by its id; an author chooses by
   *  reading it. */
  bankLabels?: Record<string, string>;
}) {
  const [showRaw, setShowRaw] = useState(false);
  const control = controlFor(def as never, slotIds(slotCount), bank);
  const setSlot = (slot: string, next: string) =>
    onChange({ ...value, slots: { ...value.slots, [slot]: next } });

  return (
    <>
      <h3>Answer key</h3>

      {control.kind === "raw" ? (
        <p className="muted">
          This type asks for a <code>{control.widget}</code> editor, which is not
          built yet. Until it is, the key goes in as JSON below.
        </p>
      ) : (
        <>
          {control.kind === "alternatives" && (
            <p className="muted">
              One row per blank. Type every spelling that should be marked
              correct, separated by a comma or a <code>|</code> —{" "}
              <strong>fourteen, 14</strong> means a student who wrote either is
              right. A key that accepts only one of them marks the other wrong.
            </p>
          )}
          {control.kind === "pick" && (
            <p className="muted">
              The letter from{" "}
              {control.source === "group.option_bank"
                ? "the question group's option bank"
                : control.source === "section.passage_version.paragraph_labels"
                  ? "the passage's paragraph labels"
                  : "this question's options"}
              {bank.length === 0
                && " — type it as it appears there, since this form does not have the bank in front of it"}
              .
            </p>
          )}

          {control.kind === "multi" ? (
            <MultiPicker control={control} value={value} onChange={onChange}
                         labels={bankLabels ?? {}} />
          ) : (
            <SlotRows control={control} value={value} setSlot={setSlot}
                      labels={bankLabels ?? {}} />
          )}

          {control.kind === "alternatives" && (
            <>
              <label className="choice">
                <input
                  type="checkbox"
                  checked={value.caseSensitive}
                  onChange={(e) => onChange({ ...value, caseSensitive: e.target.checked })}
                />
                Capital letters must match
                <span className="muted">
                  {" "}— off for almost everything; on for a proper noun where
                  case is the point
                </span>
              </label>
              <div className="row">
                <button type="button" className="link"
                        onClick={() => onSlotCount(slotCount + 1)}>
                  Add a blank
                </button>
                {slotCount > 1 && (
                  <button type="button" className="link"
                          onClick={() => onSlotCount(slotCount - 1)}>
                    Remove the last one
                  </button>
                )}
              </div>
            </>
          )}
        </>
      )}

      <button type="button" className="link small"
              onClick={() => setShowRaw((open) => !open)}>
        {showRaw ? "Hide the JSON" : "Enter the JSON myself"}
      </button>
      {showRaw && (
        <>
          <p className="muted">
            Overrides everything above when it is not empty. For pasting a key
            from somewhere else, or for a question the boxes above cannot
            describe.
          </p>
          <textarea
            id="key"
            rows={3}
            value={rawText}
            onChange={(event) => onRawText(event.target.value)}
            placeholder={'{"slots": {"s1": {"accept": ["fourteen", "14"]}}}'}
          />
        </>
      )}
    </>
  );
}

function SlotRows({ control, value, setSlot, labels }: {
  control: Extract<Control, { kind: "alternatives" | "fixed" | "pick" }>;
  value: KeyValue;
  setSlot: (slot: string, next: string) => void;
  labels: Record<string, string>;
}) {
  const show = (option: string) =>
    labels[option] ? `${option} — ${labels[option]}` : option;
  return (
    <>
      {control.slots.map((slot, index) => {
        const id = `key-${slot}`;
        const label = control.slots.length === 1
          ? "Correct answer"
          : `Blank ${index + 1}`;
        return (
          <span key={slot} className="key-row">
            <label htmlFor={id}>{label}</label>
            {control.kind === "fixed" ? (
              <select id={id} value={value.slots[slot] ?? ""}
                      onChange={(e) => setSlot(slot, e.target.value)}>
                <option value="">— choose —</option>
                {control.options.map((option) => (
                  <option key={option} value={option}>{show(option)}</option>
                ))}
              </select>
            ) : control.kind === "pick" && control.options.length > 0 ? (
              <select id={id} value={value.slots[slot] ?? ""}
                      onChange={(e) => setSlot(slot, e.target.value)}>
                <option value="">— choose —</option>
                {control.options.map((option) => (
                  <option key={option} value={option}>{show(option)}</option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                value={value.slots[slot] ?? ""}
                onChange={(e) => setSlot(slot, e.target.value)}
                placeholder={control.kind === "alternatives"
                  ? "fourteen, 14" : "A"}
              />
            )}
          </span>
        );
      })}
    </>
  );
}

function MultiPicker({ control, value, onChange, labels }: {
  control: Extract<Control, { kind: "multi" }>;
  value: KeyValue;
  onChange: (next: KeyValue) => void;
  labels: Record<string, string>;
}) {
  const options = control.options.length ? control.options : LETTERS;
  const toggle = (option: string) => {
    const held = new Set(value.correct);
    if (held.has(option)) held.delete(option); else held.add(option);
    onChange({ ...value, correct: [...held].sort() });
  };
  return (
    <>
      <p className="muted">
        Tick every option that is correct. This type scores the SET — a student
        who picks a right answer and a wrong one does not get half.
      </p>
      <div className="row">
        {options.map((option) => (
          <label key={option} className="choice">
            <input type="checkbox" checked={value.correct.includes(option)}
                   onChange={() => toggle(option)} />
            {labels[option] ? `${option} — ${labels[option]}` : option}
          </label>
        ))}
      </div>
      {value.correct.length === 1 && (
        <p className="muted">
          Only one ticked. This type expects at least two — a single answer is
          what <code>mcq_single</code> is for.
        </p>
      )}
    </>
  );
}

export { joinAlternatives };
