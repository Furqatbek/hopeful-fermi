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

export type Answers = Record<string, string>;

type Props = {
  question: Question;
  group: Group;
  answers: Answers;
  onAnswer: (slot: string, value: string) => void;
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
          value={answers[part] ?? ""}
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
              checked={answers[slot] === option.id}
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

const TFNG = ["TRUE", "FALSE", "NOT GIVEN"];
const YNNG = ["YES", "NO", "NOT GIVEN"];

export function QuestionView({ question, group, answers, onAnswer, disabled = false }: Props) {
  const p = question.payload;
  const slots = question.slot_keys ?? [];
  const first = slots[0] ?? "s1";
  const off = !!disabled;

  const limit = group.word_limit?.max_words
    ? `No more than ${group.word_limit.max_words} word${group.word_limit.max_words === 1 ? "" : "s"}` +
      (group.word_limit.allow_number ? " and/or a number" : "")
    : null;

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
            value={answers[first] ?? ""}
            onChange={(e) => onAnswer(first, e.target.value)}
            disabled={off}
            aria-label={`Question ${question.number}`}
            autoComplete="off" autoCorrect="off" spellCheck={false}
          />
        </>
      );
      break;
    }

    case "matching_information":
    case "matching_features":
    case "matching_headings": {
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
              value={answers[first] ?? ""}
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
              value={answers[first] ?? ""}
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
                value={answers[slot] ?? ""}
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
      {body}
    </article>
  );
}
