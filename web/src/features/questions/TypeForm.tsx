/**
 * A question form built from the REGISTRY, not from a switch on `type_key`.
 *
 * The registry exists so a question type can be added to a running system with
 * no migration and no redeploy — `POST /admin/question-types`, and it is live.
 * A console that hardcoded a form per type would silently revoke that: the type
 * would exist, be scorable, be composable, and be unauthorable, which is the
 * same as not existing.
 *
 * So each definition carries `authoring.form`: a list of
 * `{ field, widget, required?, optional?, label?, hint?, skills? }`, and this
 * renders it. Every widget the seventeen shipped types ask for now has a real
 * editor — the last three (`blank_editor`, `audio_timestamp`,
 * `paragraph_picker`) went in together, because between them they were the only
 * thing standing between a teacher and a publishable sentence completion.
 *
 * **Anything unrecognised still falls back to a JSON field rather than being
 * dropped.** That fallback is the load-bearing part. A type added tomorrow with
 * a widget nobody has written yet is awkward to author but not impossible, and
 * the screen says which widget is missing rather than pretending the field is
 * not there. It is a fallback, though, not a plan: a REQUIRED field behind it —
 * or a required field with no form entry at all, which is what `slots` was —
 * makes the type unpublishable from this console, and the write endpoint will
 * not say so, because the payload is checked at publish.
 *
 * What this does NOT do is validate the payload. `payload_schema` is enforced by
 * the server against the registry definition, and a second copy of those rules
 * here would be a second source of truth that drifts. The server's findings are
 * rendered instead — all of them, which is a property `ValidationFailed` goes out
 * of its way to provide.
 */

import { useState } from "react";

import { COMPOSITE, CompositeField } from "./PayloadWidgets";
import { formatTimestamp, parseTimestamp } from "./payloadParts";
import { parseBank } from "../groups/optionBank";

export type FormField = {
  field: string;
  widget: string;
  required?: boolean;
  optional?: boolean;
  multiline?: boolean;
  /** The registry's own wording, which is better than the column name. */
  label?: Record<string, string>;
  hint?: Record<string, string>;
  /** The skills this field applies to. `audio_hint_ms` is listening-only, and a
   *  cue into a recording on a Reading question is a box with no meaning. */
  skills?: string[];
};

export type Payload = Record<string, unknown>;

/** Widgets rendered natively. Everything else gets the JSON fallback. */
const NATIVE = new Set(["text", "richtext", "number", "toggle", "option_list",
                        "word_limit_builder", "audio_timestamp",
                        "paragraph_picker"]);

export function TypeForm({ fields, value, onChange, skill }: {
  fields: FormField[];
  value: Payload;
  onChange: (next: Payload) => void;
  /** The question's skill, for the fields the registry scopes to one. Omitted
   *  means "show everything", which is the honest default when the caller does
   *  not know it — a hidden field is an uneditable one. */
  skill?: string | undefined;
}) {
  const set = (field: string, next: unknown) => onChange({ ...value, [field]: next });
  // A composite widget writes MORE than its own field: the schemas for notes,
  // flowcharts, forms, tables and sentences all require a `slots` array
  // alongside the text, and it is derived from the markers rather than typed
  // twice. So those widgets need to patch several keys at once, which `set`
  // cannot express.
  const patch = (next: Payload) => onChange({ ...value, ...next });

  const shown = fields.filter((spec) =>
    !spec.skills || !skill || spec.skills.includes(skill));

  return (
    <>
      {shown.map((spec) => (
        COMPOSITE.has(spec.widget)
          ? <CompositeField key={`${spec.widget}:${spec.field}`} spec={spec}
                            payload={value} onPatch={patch} />
          : <Field key={spec.field} spec={spec} value={value[spec.field]} onChange={set} />
      ))}
    </>
  );
}

/** What to call the field. The registry carries a written label for some of
 *  them and it beats the underscored column name every time — "Sentence", not
 *  "text"; "Information to locate", not "statement". */
function labelOf(spec: FormField): string {
  return spec.label?.["en"] ?? spec.field.replaceAll("_", " ");
}

function Hint({ spec }: { spec: FormField }) {
  const hint = spec.hint?.["en"];
  return hint ? <p className="muted">{hint}</p> : null;
}

function Field({ spec, value, onChange }: {
  spec: FormField;
  value: unknown;
  onChange: (field: string, next: unknown) => void;
}) {
  const label = labelOf(spec);
  const id = `f-${spec.field}`;
  const required = spec.required === true || spec.optional === false;

  if (spec.widget === "text" || spec.widget === "richtext") {
    return (
      <>
        <label htmlFor={id}>{label}</label>
        <Hint spec={spec} />
        {spec.widget === "richtext" ? (
          <textarea
            id={id}
            rows={3}
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(spec.field, e.target.value)}
            required={required}
          />
        ) : (
          <input
            id={id}
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(spec.field, e.target.value)}
            required={required}
          />
        )}
      </>
    );
  }

  if (spec.widget === "paragraph_picker") {
    /* Which paragraph the answer is in — a single capital, per the schema's own
     * `^[A-Z]$`. A free text box let an author type "B." or "para 2", both of
     * which the publish gate refuses, and neither of which reads as a mistake
     * while typing it. Optional, so the empty choice is real and first. */
    return (
      <>
        <label htmlFor={id}>{label}</label>
        <Hint spec={spec} />
        <select
          id={id}
          value={typeof value === "string" ? value : ""}
          onChange={(e) =>
            onChange(spec.field, e.target.value === "" ? undefined : e.target.value)}
          required={required}
        >
          <option value="">— not set —</option>
          {[..."ABCDEFGHIJKLMNOPQRSTUVWXYZ"].map((letter) => (
            <option key={letter} value={letter}>{letter}</option>
          ))}
        </select>
      </>
    );
  }

  if (spec.widget === "audio_timestamp") {
    return <AudioTimestamp spec={spec} value={value} onChange={onChange} />;
  }

  if (spec.widget === "number") {
    return (
      <>
        <label htmlFor={id}>{label}</label>
        <input
          id={id}
          inputMode="numeric"
          value={value == null ? "" : String(value)}
          // Empty stays UNDEFINED rather than becoming 0. An optional numeric
          // field sent as 0 is a different question from one not sent at all,
          // and `max_words: 0` would refuse every answer.
          onChange={(e) =>
            onChange(spec.field, e.target.value === "" ? undefined : Number(e.target.value))
          }
          required={required}
        />
      </>
    );
  }

  if (spec.widget === "toggle") {
    return (
      <label className="choice">
        <input
          type="checkbox"
          checked={value === true}
          onChange={(e) => onChange(spec.field, e.target.checked)}
        />
        {label}
      </label>
    );
  }

  if (spec.widget === "option_list") {
    /* `[{id, text}]`, not `string[]`.
     *
     * It sent bare strings, and `payload_schema` for `mcq_single` and
     * `mcq_multi` requires objects with an `id` matching `^[A-H]$`. The server
     * only checks the payload at PUBLISH, so `POST /questions` answered 201 and
     * the question sat in the library looking finished until the publish gate
     * refused it with `PAYLOAD_INVALID` — and the author could not fix it here,
     * because this widget had no way to express an id at all.
     *
     * The letters come from position, as they do for a group's option bank, and
     * a list pasted with its letters already attached is stripped and
     * renumbered. Nobody types "A =". */
    const options = Array.isArray(value)
      ? (value as (string | { id?: string; text?: string })[]) : [];
    const asText = options
      .map((option) => (typeof option === "string" ? option : option.text ?? ""))
      .join("\n");
    return (
      <>
        <label htmlFor={id}>{label} — one per line, just the words</label>
        <textarea
          id={id}
          rows={4}
          value={asText}
          onChange={(e) => onChange(spec.field, parseBank(e.target.value, "letters"))}
          required={required}
        />
        <OptionPreview options={options} />
      </>
    );
  }

  if (spec.widget === "word_limit_builder") {
    const limit = (value ?? {}) as Record<string, unknown>;
    return (
      <fieldset>
        <legend>{label}</legend>
        <label htmlFor={`${id}-max`}>Maximum words</label>
        <input
          id={`${id}-max`}
          inputMode="numeric"
          value={limit["max_words"] == null ? "" : String(limit["max_words"])}
          onChange={(e) =>
            onChange(spec.field, {
              ...limit,
              max_words: e.target.value === "" ? undefined : Number(e.target.value),
            })
          }
        />
        <label className="choice">
          <input
            type="checkbox"
            checked={limit["allow_number"] === true}
            onChange={(e) => onChange(spec.field, { ...limit, allow_number: e.target.checked })}
          />
          A number counts as one word
        </label>
        <label className="choice">
          <input
            type="checkbox"
            checked={limit["hyphen_counts_as_one"] === true}
            onChange={(e) =>
              onChange(spec.field, { ...limit, hyphen_counts_as_one: e.target.checked })
            }
          />
          A hyphenated pair counts as one, as in real IELTS marking
        </label>
      </fieldset>
    );
  }

  return <JsonField spec={spec} value={value} onChange={onChange} />;
}

/**
 * Where in the recording the answer is spoken.
 *
 * The schema wants milliseconds from the start of the track, which is not a
 * number any teacher has: they have a player showing `1:32`. So the box takes
 * `m:ss`, and a bare number as seconds — somebody typing `90` into a box
 * labelled with a time means a minute and a half, not a tenth of a second.
 *
 * The draft is held here rather than derived from the payload, because
 * `formatTimestamp` would rewrite `1:3` to `0:01` in the middle of typing
 * `1:30`.
 */
function AudioTimestamp({ spec, value, onChange }: {
  spec: FormField;
  value: unknown;
  onChange: (field: string, next: unknown) => void;
}) {
  const [text, setText] = useState(() => formatTimestamp(value));
  const id = `f-${spec.field}`;
  const unreadable = text.trim() !== "" && parseTimestamp(text) === undefined;

  return (
    <>
      {/* Not `labelOf`: the field is called `audio_hint_ms` and the box does not
          take milliseconds. A label naming a unit the input refuses is worse
          than a generic one. The registry's own label wins if it ever grows one. */}
      <label htmlFor={id}>{spec.label?.["en"] ?? "Where in the recording"}</label>
      <p className="muted">
        {spec.hint?.["en"]
          ?? "As m:ss, or a number of seconds. Optional — it is a cue for whoever "
             + "edits the paper, not something a student sees."}
      </p>
      <input
        id={id}
        className={unreadable ? "invalid" : undefined}
        value={text}
        placeholder="1:32"
        onChange={(event) => {
          setText(event.target.value);
          // An unreadable draft clears the field rather than keeping a stale
          // number: half-typed `1:` must not leave the previous cue in the
          // payload, looking saved.
          onChange(spec.field, parseTimestamp(event.target.value));
        }}
      />
      {unreadable && <p className="error">Not a time yet — write it as m:ss, or seconds.</p>}
    </>
  );
}

/**
 * The fallback, and the reason a new question type is still authorable the day
 * it is registered.
 *
 * It names the widget it could not render, so the gap is a to-do rather than a
 * mystery, and it keeps the last VALID text on a parse error instead of throwing
 * away what was typed — losing a half-built hotspot list to a stray comma would
 * be worse than showing the error and waiting.
 */
function JsonField({ spec, value, onChange }: {
  spec: FormField;
  value: unknown;
  onChange: (field: string, next: unknown) => void;
}) {
  const [text, setText] = useState(() =>
    value === undefined ? "" : JSON.stringify(value, null, 2));
  const [invalid, setInvalid] = useState(false);
  const id = `f-${spec.field}`;

  return (
    <>
      <label htmlFor={id}>
        {labelOf(spec)} <span className="muted">(JSON)</span>
      </label>
      <p className="muted">
        No editor for the <code>{spec.widget}</code> widget yet — enter the value
        as JSON. The server validates it against the type's <code>payload_schema</code>
        either way.
      </p>
      <textarea
        id={id}
        rows={4}
        className={invalid ? "invalid" : undefined}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          if (event.target.value.trim() === "") {
            setInvalid(false);
            onChange(spec.field, undefined);
            return;
          }
          try {
            onChange(spec.field, JSON.parse(event.target.value));
            setInvalid(false);
          } catch {
            setInvalid(true);
          }
        }}
      />
      {invalid && <p className="error">Not valid JSON yet — the last valid value is kept.</p>}
    </>
  );
}

export { NATIVE as NATIVE_WIDGETS };


/** The letters this question will offer, shown because the author no longer
 *  types them and the answer key is about to ask which one is right. */
function OptionPreview({ options }: {
  options: (string | { id?: string; text?: string })[];
}) {
  const shaped = options.filter((o): o is { id?: string; text?: string } =>
    typeof o === "object" && o !== null);
  if (shaped.length === 0) return null;
  return (
    <ul className="bank-preview">
      {shaped.map((option, index) => (
        <li key={option.id ?? index}><b>{option.id}</b> {option.text}</li>
      ))}
    </ul>
  );
}
