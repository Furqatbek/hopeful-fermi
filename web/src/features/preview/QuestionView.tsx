/**
 * One question, as an entrant meets it.
 *
 * Same philosophy as `TypeForm` on the authoring side and for the same reason:
 * the registry exists so a question type can be added to a running system with
 * no deploy, and a renderer that switched on `type_key` would quietly revoke
 * that — the type would be authorable, composable, scorable and unpreviewable.
 *
 * So this renders the SHAPES a payload comes in rather than the types:
 *
 *   * `{{s1}}` blanks in a body of text — the completion family, and the only
 *     shape where position matters, because a blank in the wrong sentence is the
 *     defect preview exists to catch;
 *   * a stem with its own options — the MCQ family;
 *   * a group-level option bank — the matching family, where every question in
 *     the set chooses from one shared list;
 *   * anything else — a plain box per slot, with the payload shown, because an
 *     author previewing an unfamiliar type still needs to see what a student
 *     would be asked and whether it can be answered at all.
 *
 * The fallback is the load-bearing part. It is honest about not knowing the
 * type, which is far better than rendering nothing and letting the author
 * conclude the question is broken.
 */

export interface Question {
  number?: number;
  question_version_xid?: string;
  type_key?: string;
  payload?: Record<string, unknown>;
  slot_keys?: string[];
}

export interface Option {
  id?: string;
  text?: string;
}

/** The prose a question is asked in, whatever the type calls its field. */
function prompt(payload: Record<string, unknown>): string | null {
  for (const field of ["text", "summary", "statement", "stem", "question", "prompt"]) {
    const value = payload[field];
    if (typeof value === "string") return value;
  }
  return null;
}

function optionsOf(payload: Record<string, unknown>): Option[] {
  const raw = payload["options"];
  return Array.isArray(raw) ? (raw as Option[]) : [];
}

export function QuestionView({ question, bank, answers, onAnswer }: {
  question: Question;
  bank: Option[];
  answers: Record<string, string>;
  onAnswer: (slotKey: string, value: string) => void;
}) {
  const payload = question.payload ?? {};
  const slots = question.slot_keys ?? [];
  const text = prompt(payload);
  const options = optionsOf(payload);
  const choices = options.length > 0 ? options : bank;

  // The completion family: split on the blanks so each input sits where the gap
  // is. Numbering comes from the SNAPSHOT — a blank rendered in the wrong place
  // is exactly what an author is previewing for.
  if (text && /\{\{s\d+\}\}/.test(text)) {
    const parts = text.split(/(\{\{s\d+\}\})/g);
    return (
      <li>
        <span className="num">{question.number}</span>{" "}
        <span className="q-body">
          {parts.map((part, index) => {
            const blank = part.match(/^\{\{(s\d+)\}\}$/);
            if (!blank) return <span key={index}>{part}</span>;
            const slot = blank[1]!;
            return (
              <input
                key={index}
                className="blank"
                value={answers[slot] ?? ""}
                onChange={(event) => onAnswer(slot, event.target.value)}
                aria-label={`Blank ${slot}`}
              />
            );
          })}
        </span>
      </li>
    );
  }

  if (choices.length > 0 && slots.length > 0) {
    return (
      <li>
        <span className="num">{question.number}</span>{" "}
        <span className="q-body">
          {text}
          {slots.map((slot) => (
            <select
              key={slot}
              value={answers[slot] ?? ""}
              onChange={(event) => onAnswer(slot, event.target.value)}
              aria-label={`Answer ${slot}`}
            >
              <option value="">—</option>
              {choices.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.id}{option.text && option.text !== option.id
                    ? ` · ${option.text}` : ""}
                </option>
              ))}
            </select>
          ))}
        </span>
      </li>
    );
  }

  return (
    <li>
      <span className="num">{question.number}</span>{" "}
      <span className="q-body">
        {text ?? (
          <em className="muted">
            This type has no prompt field this preview recognises.
          </em>
        )}
        {slots.map((slot) => (
          <input
            key={slot}
            className="blank"
            value={answers[slot] ?? ""}
            onChange={(event) => onAnswer(slot, event.target.value)}
            aria-label={`Answer ${slot}`}
          />
        ))}
        {!text && (
          /* Shown rather than swallowed: an author previewing a type this does
             not know still has to be able to see what a student is asked. */
          <details className="muted">
            <summary>{question.type_key} payload</summary>
            <pre>{JSON.stringify(payload, null, 1)}</pre>
          </details>
        )}
      </span>
    </li>
  );
}
