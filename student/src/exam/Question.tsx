/**
 * Rendering a question, whatever type it is.
 *
 * The registry has seventeen types and **sixteen of them answer the same way**:
 * a `slots` object mapping slot key to a string. Only `mcq_multi` differs, with
 * a `selected` array. So this is one slot-based renderer with per-type
 * presentation, rather than seventeen components — and a new type that composes
 * existing primitives renders here with no code change, which is the promise
 * ADR-0001 §8.3 makes about the registry.
 *
 * ── the answer shape is the server's, not ours ──────────────────────────────
 *
 * A delta is per SLOT — `{question_version_xid, slot_key, response}` — so this
 * reports one change at a time and never assembles a whole-question answer. That
 * matches `response_schema` exactly, and it is what lets a half-finished
 * table-completion save the cells that are done.
 *
 * ── the word limit is displayed, never enforced ─────────────────────────────
 *
 * "NO MORE THAN TWO WORDS" is a MARKING rule, and the real exam lets you exceed
 * it and simply marks you wrong (ADR-0001 §8.9). Truncating input here would
 * hide the mistake the student needs to learn not to make, and would disagree
 * with the server, which is the only thing that marks.
 */

import { type ReactNode } from "react";

export type Question = {
  number: number;
  question_version_xid: string;
  type_key: string;
  payload: Record<string, unknown>;
  slot_keys?: string[];
};

export type Group = {
  number_start?: number;
  instructions?: Record<string, string>;
  word_limit?: { max_words?: number; allow_number?: boolean } | null;
  option_bank?: { id: string; text: string }[];
  questions: Question[];
};

/**
 * A slot's value, keyed `${question_version_xid}:${slot}`.
 *
 * `string | string[]` and not `string`, because that is what the wire is:
 * `Delta.response` has always been `string | string[] | null`, and `mcq_multi`
 * sends a list. Narrowing this to `string` was what made a multi-select
 * unanswerable — the type forced the renderer into the generic default branch,
 * which sends a string the server's derived slot schema rejects outright.
 */
export type Answers = Record<string, string | string[]>;

/** The value as a text box wants it. A list here is a programming error, not a
 *  student one, so it renders empty rather than `"A,B"`. */
function asText(value: string | string[] | undefined): string {
  return typeof value === "string" ? value : "";
}

/** The value as a multi-select wants it. */
export function asList(value: string | string[] | undefined): string[] {
  return Array.isArray(value) ? value : value ? [value] : [];
}

/**
 * The paragraph slots of a `matching_headings` payload, in payload order.
 *
 * The registry declares `slots: [{key: "s1", paragraph: "A"}, ...]` with up to
 * fourteen entries, and the scorer marks each one on its own (`aggregate:
 * per_slot`). Rendering the type through the single-slot matching branch bound
 * ONE control to `slots[0]`, so every paragraph after the first was
 * unanswerable and scored zero — the defect docs/known-issues.md records under
 * "matching_headings answers only its first paragraph".
 *
 * The payload is `Record<string, unknown>` on the wire, so entries are checked
 * rather than cast: a malformed or missing `slots` list falls back to the
 * question's own `slot_keys`, labelled with the key itself, which still lets
 * every slot be answered rather than silently dropping the item.
 */
export function paragraphSlots(
  payload: Record<string, unknown>, slotKeys: readonly string[],
): { key: string; paragraph: string }[] {
  const raw = payload["slots"];
  if (Array.isArray(raw)) {
    const entries = raw.flatMap((entry) => {
      if (typeof entry !== "object" || entry === null) return [];
      const { key, paragraph } = entry as { key?: unknown; paragraph?: unknown };
      return typeof key === "string" && typeof paragraph === "string"
        ? [{ key, paragraph }] : [];
    });
    if (entries.length > 0) return entries;
  }
  return slotKeys.map((key) => ({ key, paragraph: key }));
}

/**
 * The group's instruction line — the rubric printed above a set on the real
 * paper ("Choose the correct heading for each paragraph from the list...").
 *
 * The snapshot has always carried it (`instructions` per group), and the
 * console preview draws it, but the exam renderer never read it, so a student
 * saw fourteen selects with no sentence saying what to do with them. `en`
 * first because the paper is English; otherwise the first locale that holds a
 * string. The map is open on the wire — `{}` is a legal value and so, to the
 * type system, is anything — so non-strings are skipped rather than rendered
 * as "[object Object]" (the same reasoning as the console's Composition.tsx).
 */
export function rubricOf(instructions: Record<string, unknown> | undefined): string | null {
  if (!instructions) return null;
  const en = instructions["en"];
  if (typeof en === "string" && en.length > 0) return en;
  const first = Object.values(instructions)
    .find((value): value is string => typeof value === "string" && value.length > 0);
  return first ?? null;
}

/**
 * Add or remove one option from a selection, keeping the options' own order.
 *
 * The set is compared unordered when it is marked, so ordering is presentation
 * only — but it is the order the answer is played back in on the review screen,
 * and "B, D" reading as "D, B" because that was the click order looks like a
 * different answer to the student who chose it.
 */
export function toggleSelection(
  chosen: readonly string[], id: string, optionIds: readonly string[],
): string[] {
  if (chosen.includes(id)) return chosen.filter((c) => c !== id);
  return optionIds.filter((o) => o === id || chosen.includes(o));
}

type Props = {
  question: Question;
  group: Group;
  answers: Answers;
  onAnswer: (slot: string, value: string | string[]) => void;
  disabled?: boolean;
};

/** `{{s1}}` placeholders become inputs, in place, inside the sentence. */
function withSlots(
  text: string, q: Question, answers: Answers,
  onAnswer: Props["onAnswer"], disabled: boolean,
): ReactNode[] {
  // Split on the placeholder and keep the captured slot name, so the sentence
  // reads as one line with the box sitting where the gap is — which is how the
  // real client presents completion items, and it is materially easier to read
  // than a sentence followed by a detached input.
  const parts = text.split(/\{\{(\w+)\}\}/g);
  return parts.map((part, i) =>
    i % 2 === 0
      ? <span key={i}>{part}</span>
      : (
        <input
          key={i}
          className="q__gap"
          value={asText(answers[part])}
          onChange={(e) => onAnswer(part, e.target.value)}
          disabled={disabled}
          aria-label={`Question ${q.number}, gap ${part}`}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
        />
      ),
  );
}

function Choices({ question, answers, onAnswer, disabled, options, slot }: {
  question: Question; answers: Answers; onAnswer: Props["onAnswer"];
  disabled: boolean; options: { id: string; text: string }[]; slot: string;
}) {
  return (
    <ul className="q__options">
      {options.map((option) => (
        <li key={option.id}>
          <label className="q__option">
            <input
              type="radio"
              name={`${question.question_version_xid}:${slot}`}
              checked={asText(answers[slot]) === option.id}
              onChange={() => onAnswer(slot, option.id)}
              disabled={disabled}
            />
            <span className="q__option-id">{option.id}</span>
            <span>{option.text}</span>
          </label>
        </li>
      ))}
    </ul>
  );
}

/**
 * Choose K of N. The one type whose answer is a list.
 *
 * ── why the count is shown and not enforced ─────────────────────────────────
 *
 * Selecting more than `select_count` scores the WHOLE item zero — not the
 * nearest K, not partial credit. That is the real exam's rule and the scorer
 * implements it, so it is the single most expensive mistake available on this
 * screen and the student has to be able to see themselves making it.
 *
 * Refusing the extra click would hide it, exactly as truncating an over-long
 * completion answer would hide a word-limit breach — the rule this file already
 * follows. So the count is live, the warning is loud, and the click is allowed:
 * a student who learns "TWO means two" here does not learn it in the test.
 */
function MultiChoice({ question, answers, onAnswer, disabled, options, slot, expect }: {
  question: Question; answers: Answers; onAnswer: Props["onAnswer"];
  disabled: boolean; options: { id: string; text: string }[];
  slot: string; expect: number;
}) {
  const chosen = asList(answers[slot]);
  const over = expect > 0 && chosen.length > expect;

  const toggle = (id: string) =>
    onAnswer(slot, toggleSelection(chosen, id, options.map((o) => o.id)));

  return (
    <>
      <ul className="q__options q__options--multi">
        {options.map((option) => (
          <li key={option.id}>
            <label className="q__option">
              <input
                type="checkbox"
                name={`${question.question_version_xid}:${slot}`}
                checked={chosen.includes(option.id)}
                onChange={() => toggle(option.id)}
                disabled={disabled}
              />
              <span className="q__option-id">{option.id}</span>
              <span>{option.text}</span>
            </label>
          </li>
        ))}
      </ul>
      <p className={over ? "q__count q__count--over" : "q__count"} aria-live="polite">
        {over
          ? `You have chosen ${chosen.length}. Choosing more than ${expect} scores zero for this question.`
          : `Chosen ${chosen.length} of ${expect}.`}
      </p>
    </>
  );
}

const TFNG = ["TRUE", "FALSE", "NOT GIVEN"];
const YNNG = ["YES", "NO", "NOT GIVEN"];

export function QuestionView({ question, group, answers, onAnswer, disabled = false }: Props) {
  const p = question.payload;
  const slots = question.slot_keys ?? [];
  const first = slots[0] ?? "s1";
  const off = !!disabled;

  // The word limit is a GROUP rule and only some of the group's questions can
  // breach it. "No more than two words" above a list of checkboxes is not just
  // noise — it tells a student to write something on a question that has no
  // writing in it. The scorer agrees: `apply_word_limit` is set only on the
  // text-answer types.
  const TEXT_ANSWER = new Set([
    "sentence_completion", "summary_completion", "note_completion",
    "table_completion", "form_completion", "flowchart_completion",
    "diagram_completion", "short_answer",
  ]);
  const limit = group.word_limit?.max_words && TEXT_ANSWER.has(question.type_key)
    ? `No more than ${group.word_limit.max_words} word${group.word_limit.max_words === 1 ? "" : "s"}` +
      (group.word_limit.allow_number ? " and/or a number" : "")
    : null;

  // Shown on EVERY question of the set, not only at `number_start`: the runner
  // shows one question at a time, so a rubric drawn once would be off-screen
  // for every question but the first.
  const rubric = rubricOf(group.instructions);

  let body: ReactNode;

  switch (question.type_key) {
    case "true_false_notgiven":
    case "yes_no_notgiven": {
      const choices = question.type_key === "true_false_notgiven" ? TFNG : YNNG;
      body = (
        <>
          <p className="q__stem">{String(p["statement"] ?? "")}</p>
          <Choices
            question={question} answers={answers} onAnswer={onAnswer} disabled={off}
            slot={first}
            options={choices.map((c) => ({ id: c, text: c }))}
          />
        </>
      );
      break;
    }

    case "mcq_single": {
      const options = (p["options"] as { id: string; text: string }[] | undefined) ?? [];
      body = (
        <>
          <p className="q__stem">{String(p["stem"] ?? "")}</p>
          <Choices
            question={question} answers={answers} onAnswer={onAnswer} disabled={off}
            slot={first} options={options}
          />
        </>
      );
      break;
    }

    case "mcq_multi": {
      const options = (p["options"] as { id: string; text: string }[] | undefined) ?? [];
      const expect = Number(p["select_count"] ?? 2);
      body = (
        <>
          <p className="q__stem">{String(p["stem"] ?? "")}</p>
          {/* The instruction the real paper prints above the options, derived
              rather than authored, so it can never disagree with the key. */}
          <p className="q__instruction">
            Choose <b>{expect === 2 ? "TWO" : expect === 3 ? "THREE" : expect}</b>{" "}
            {expect === 1 ? "letter" : "letters"}.
          </p>
          <MultiChoice
            question={question} answers={answers} onAnswer={onAnswer} disabled={off}
            slot={first} options={options} expect={expect}
          />
        </>
      );
      break;
    }

    case "sentence_completion":
    case "summary_completion":
    case "summary_completion_bank": {
      const text = String(p["text"] ?? p["summary"] ?? "");
      body = <p className="q__stem">{withSlots(text, question, answers, onAnswer, off)}</p>;
      break;
    }

    case "short_answer": {
      body = (
        <>
          <p className="q__stem">{String(p["question"] ?? "")}</p>
          <input
            className="q__answer"
            value={asText(answers[first])}
            onChange={(e) => onAnswer(first, e.target.value)}
            disabled={off}
            aria-label={`Question ${question.number}`}
            autoComplete="off" autoCorrect="off" spellCheck={false}
          />
        </>
      );
      break;
    }

    case "matching_headings": {
      // One question, many paragraphs: the payload lists every paragraph to be
      // matched and each is its own slot with its own running number (the
      // server numbers `question.number + slot index`, the same arithmetic as
      // here). The headings come from the GROUP's option bank, as for the
      // single-slot matching types below.
      const bank = group.option_bank ?? [];
      const paragraphs = paragraphSlots(p, slots);
      body = (
        <>
          {paragraphs.map((s, i) => (
            <label key={s.key} className="q__slot">
              <span className="q__slot-key">
                {question.number + i}. Paragraph {s.paragraph}
              </span>
              {bank.length > 0 ? (
                <select
                  className="q__select"
                  value={asText(answers[s.key])}
                  onChange={(e) => onAnswer(s.key, e.target.value)}
                  disabled={off}
                  aria-label={`Question ${question.number + i}, paragraph ${s.paragraph}`}
                >
                  <option value="">—</option>
                  {bank.map((o) => <option key={o.id} value={o.id}>{o.id}. {o.text}</option>)}
                </select>
              ) : (
                <input
                  className="q__answer"
                  value={asText(answers[s.key])}
                  onChange={(e) => onAnswer(s.key, e.target.value)}
                  disabled={off}
                  aria-label={`Question ${question.number + i}, paragraph ${s.paragraph}`}
                />
              )}
            </label>
          ))}
        </>
      );
      break;
    }

    case "matching_information":
    case "matching_features": {
      // The choices come from the GROUP's option bank, which is what makes a
      // matching set a set: every question in it draws from one list.
      const bank = group.option_bank ?? [];
      body = (
        <>
          <p className="q__stem">
            {String(p["statement"] ?? p["stem"] ?? `Question ${question.number}`)}
          </p>
          {bank.length > 0 ? (
            <select
              className="q__select"
              value={asText(answers[first])}
              onChange={(e) => onAnswer(first, e.target.value)}
              disabled={off}
              aria-label={`Question ${question.number}`}
            >
              <option value="">—</option>
              {bank.map((o) => <option key={o.id} value={o.id}>{o.id}. {o.text}</option>)}
            </select>
          ) : (
            <input
              className="q__answer"
              value={asText(answers[first])}
              onChange={(e) => onAnswer(first, e.target.value)}
              disabled={off}
              aria-label={`Question ${question.number}`}
            />
          )}
        </>
      );
      break;
    }

    default: {
      // Every remaining type is slot-based: note, table, form, flowchart,
      // diagram, map. Their payloads differ in how the PROMPT is laid out, not
      // in how an answer is given, so a labelled box per slot is correct if
      // plain — and correct-but-plain beats a blank screen for a type nobody
      // has styled yet. The layouts land per type; the answering already works.
      const title = String(p["title"] ?? p["caption"] ?? "");
      body = (
        <>
          {title && <p className="q__stem">{title}</p>}
          {slots.length === 0 && <p className="muted">This question type has no answer slots.</p>}
          {slots.map((slot) => (
            <label key={slot} className="q__slot">
              <span className="q__slot-key">{slot}</span>
              <input
                value={asText(answers[slot])}
                onChange={(e) => onAnswer(slot, e.target.value)}
                disabled={off}
                aria-label={`Question ${question.number}, ${slot}`}
                autoComplete="off" autoCorrect="off" spellCheck={false}
              />
            </label>
          ))}
        </>
      );
    }
  }

  return (
    <article className="q" id={`q-${question.number}`}>
      <div className="q__head">
        <span className="q__number">{question.number}</span>
        {limit && <span className="q__limit">{limit}</span>}
      </div>
      {rubric && <p className="q__instruction">{rubric}</p>}
      {body}
    </article>
  );
}
