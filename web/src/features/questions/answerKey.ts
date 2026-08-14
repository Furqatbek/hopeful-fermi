/**
 * Turning a question type's declared `key_widget` into an answer key, without
 * anybody typing JSON.
 *
 * Every definition in the registry already says which editor it wants —
 * `authoring.key_widget`, ten distinct values across the seventeen types that
 * ship. The console read none of them and showed a raw JSON textarea instead,
 * so authoring one question meant hand-writing
 * `{"slots": {"s1": {"accept": ["fourteen", "14"]}}}`. That is not a thing an
 * English teacher should ever see, and the registry never asked them to.
 *
 * Pure and separate from the screen for the reason the rest of this module is:
 * the mapping from a widget to a key is a claim about the CONTRACT — what the
 * server will accept — and it should be readable and testable without a browser.
 *
 * Two key shapes exist, and only two:
 *
 *   `{ slots: { s1: { accept: [...] } } }`   sixteen types
 *   `{ correct: ["A", "C"] }`                 mcq_multi alone, whose scoring is
 *                                             `set_selection` over the whole
 *                                             question rather than per slot
 *
 * Nothing here validates. `key_schema` is enforced by the server against the
 * registry, and a second copy of those rules in the console is a second source
 * of truth that drifts — the same reasoning `TypeForm` gives for not validating
 * payloads. This builds the shape; the server judges it.
 */

/** What kind of control a teacher should get. */
export type Control =
  /** Free text, one row per blank, several accepted spellings each. */
  | { kind: "alternatives"; slots: string[]; multi: true }
  /** One fixed answer from a closed list — TRUE / FALSE / NOT GIVEN. */
  | { kind: "fixed"; slots: string[]; options: string[] }
  /** One letter per blank, out of a bank the group or payload supplies. */
  | { kind: "pick"; slots: string[]; options: string[]; source: string }
  /** Several correct options for the whole question. mcq_multi only. */
  | { kind: "multi"; options: string[] }
  /** No widget written for this type yet. */
  | { kind: "raw"; widget: string };

type Definition = {
  authoring?: { key_widget?: string } | null;
  scoring?: {
    primitive?: string;
    options?: {
      option_source?: string;
      fixed_options?: { id?: string; text?: string }[];
    } | null;
  } | null;
};

/** Widgets that mean "type the accepted answers". */
const ALTERNATIVES = new Set(["alternatives_editor", "slot_alternatives_grid"]);
/** Widgets that mean "choose one out of a bank of options". */
const PICKERS = new Set(["slot_to_bank_grid", "option_picker",
                         "paragraph_to_heading_grid", "paragraph_picker",
                         "single_option_picker"]);

/**
 * Which control this type's answer key needs.
 *
 * Driven by the DECLARED widget, not by `type_key`. A type added to a running
 * system through `POST /admin/question-types` gets the right editor without a
 * redeploy, which is the bet the registry exists to make — and an unrecognised
 * widget falls back to `raw` rather than being dropped, so the type stays
 * authorable while its editor is unwritten.
 */
export function controlFor(def: Definition | null | undefined,
                           slots: string[],
                           bank: string[] = []): Control {
  const widget = def?.authoring?.key_widget ?? "";
  const scoring = def?.scoring ?? {};
  const options = scoring.options ?? {};

  if (ALTERNATIVES.has(widget)) return { kind: "alternatives", slots, multi: true };

  if (widget === "tfng_picker" || widget === "ynng_picker") {
    // The list is data on the type, not a constant here: `fixed_options` is
    // what the scorer matches against, and a second copy in the console is how
    // a picker offers an answer the marker will refuse.
    const fixed = (options.fixed_options ?? [])
      .map((o) => o.id ?? o.text ?? "")
      .filter(Boolean);
    return { kind: "fixed", slots: slots.slice(0, 1), options: fixed };
  }

  if (widget === "multi_option_picker" || scoring.primitive === "set_selection") {
    // A–J because that is exactly what the key schema's pattern allows, and an
    // author picking K would be told so only after saving.
    return { kind: "multi", options: bank.length ? bank : LETTERS };
  }

  if (PICKERS.has(widget)) {
    return {
      kind: "pick",
      slots: widget === "paragraph_picker" || widget === "single_option_picker"
        ? slots.slice(0, 1) : slots,
      options: bank,
      source: options.option_source ?? "",
    };
  }

  return { kind: "raw", widget: widget || "none declared" };
}

export const LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"];

/** `["s1", "s2", …]`, the ids the response and key schemas both use. */
export function slotIds(count: number): string[] {
  return Array.from({ length: Math.max(1, count) }, (_, i) => `s${i + 1}`);
}

/**
 * Split what a teacher typed into the alternatives the key accepts.
 *
 * A comma OR a pipe, because both are what people reach for and the CSV import
 * path already treats `|` as the separator — an author who has seen a template
 * will type pipes, one who has not will type commas, and neither should be
 * wrong. Blank entries are dropped rather than becoming an empty accepted
 * answer, which would match a student who wrote nothing.
 */
export function splitAlternatives(text: string): string[] {
  return text.split(/[|,]/).map((part) => part.trim()).filter(Boolean);
}

export function joinAlternatives(values: string[]): string {
  return values.join(" | ");
}

export type KeyValue = {
  /** Per slot, what the teacher entered. Free text or a chosen option. */
  slots: Record<string, string>;
  /** mcq_multi only. */
  correct: string[];
  caseSensitive: boolean;
};

export function emptyValue(): KeyValue {
  return { slots: {}, correct: [], caseSensitive: false };
}

/**
 * The answer key as the server takes it, or null when nothing is filled in.
 *
 * Null rather than an empty key: `POST /questions` treats a missing key as "no
 * key yet", which is a legitimate state for a draft, while `{"slots": {}}`
 * fails `minProperties` and reads to the author as a bug in the form.
 */
export function toKey(control: Control, value: KeyValue): object | null {
  if (control.kind === "multi") {
    return value.correct.length ? { correct: [...value.correct].sort() } : null;
  }
  if (control.kind === "raw") return null;

  const slots: Record<string, object> = {};
  for (const slot of control.slots) {
    const raw = (value.slots[slot] ?? "").trim();
    if (!raw) continue;
    const accept = control.kind === "alternatives" ? splitAlternatives(raw) : [raw];
    if (!accept.length) continue;
    slots[slot] = control.kind === "alternatives" && value.caseSensitive
      ? { accept, case_sensitive: true }
      : { accept };
  }
  return Object.keys(slots).length ? { slots } : null;
}

/**
 * Read an existing key back into the editor.
 *
 * So the widget can take over from a key that already exists — one written as
 * JSON before this form existed, or imported from a CSV — instead of silently
 * discarding it the moment somebody opens the question.
 */
export function fromKey(key: unknown): KeyValue {
  const value = emptyValue();
  if (!key || typeof key !== "object") return value;
  const record = key as Record<string, unknown>;

  if (Array.isArray(record.correct)) {
    value.correct = record.correct.filter((c): c is string => typeof c === "string");
    return value;
  }

  const slots = record.slots;
  if (!slots || typeof slots !== "object") return value;
  for (const [slot, spec] of Object.entries(slots as Record<string, unknown>)) {
    if (!spec || typeof spec !== "object") continue;
    const inner = spec as Record<string, unknown>;
    const accept = Array.isArray(inner.accept)
      ? inner.accept.filter((a): a is string => typeof a === "string") : [];
    value.slots[slot] = joinAlternatives(accept);
    if (inner.case_sensitive === true) value.caseSensitive = true;
  }
  return value;
}

/** How many slots an existing key describes, so the grid opens the right size. */
export function slotCountOf(key: unknown): number {
  if (!key || typeof key !== "object") return 1;
  const slots = (key as Record<string, unknown>).slots;
  if (!slots || typeof slots !== "object") return 1;
  return Math.max(1, Object.keys(slots).length);
}
