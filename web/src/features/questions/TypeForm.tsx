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
 * `{ field, widget, required?, optional? }`, and this renders it. Seventeen types
 * ship today across nineteen widgets; the common ones are implemented properly
 * and **anything unrecognised falls back to a JSON field rather than being
 * dropped**. That fallback is the load-bearing part. A type added tomorrow with
 * a widget nobody has written yet is awkward to author but not impossible, and
 * the screen says which widget is missing rather than pretending the field is
 * not there.
 *
 * What this does NOT do is validate the payload. `payload_schema` is enforced by
 * the server against the registry definition, and a second copy of those rules
 * here would be a second source of truth that drifts. The server's findings are
 * rendered instead — all of them, which is a property `ValidationFailed` goes out
 * of its way to provide.
 */

import { useState } from "react";

import { COMPOSITE, CompositeField } from "./PayloadWidgets";

export type FormField = {
  field: string;
  widget: string;
  required?: boolean;
  optional?: boolean;
  hint?: Record<string, string>;
};

export type Payload = Record<string, unknown>;

/** Widgets rendered natively. Everything else gets the JSON fallback. */
const NATIVE = new Set(["text", "richtext", "number", "toggle", "option_list",
                        "word_limit_builder"]);

export function TypeForm({ fields, value, onChange }: {
  fields: FormField[];
  value: Payload;
  onChange: (next: Payload) => void;
}) {
  const set = (field: string, next: unknown) => onChange({ ...value, [field]: next });
  // A composite widget writes MORE than its own field: the schemas for notes,
  // flowcharts, forms and tables all require a `slots` array alongside the
  // text, and it is derived from the markers rather than typed twice. So those
  // widgets need to patch several keys at once, which `set` cannot express.
  const patch = (next: Payload) => onChange({ ...value, ...next });

  return (
    <>
      {fields.map((spec) => (
        COMPOSITE.has(spec.widget)
          ? <CompositeField key={`${spec.widget}:${spec.field}`} spec={spec}
                            payload={value} onPatch={patch} />
          : <Field key={spec.field} spec={spec} value={value[spec.field]} onChange={set} />
      ))}
    </>
  );
}

function Field({ spec, value, onChange }: {
  spec: FormField;
  value: unknown;
  onChange: (field: string, next: unknown) => void;
}) {
  const label = spec.field.replaceAll("_", " ");
  const id = `f-${spec.field}`;
  const required = spec.required === true || spec.optional === false;

  if (spec.widget === "text" || spec.widget === "richtext") {
    return (
      <>
        <label htmlFor={id}>{label}</label>
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
    const options = Array.isArray(value) ? (value as string[]) : [];
    return (
      <>
        <label htmlFor={id}>{label} — one per line</label>
        <textarea
          id={id}
          rows={4}
          value={options.join("\n")}
          onChange={(e) =>
            onChange(spec.field, e.target.value.split("\n").filter((line) => line.trim()))
          }
          required={required}
        />
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
        {spec.field.replaceAll("_", " ")} <span className="muted">(JSON)</span>
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
