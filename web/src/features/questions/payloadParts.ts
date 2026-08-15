/**
 * The seven payload builders the registry asks for and the console never had.
 *
 * `TypeForm` renders six widgets natively and drops everything else into a JSON
 * textarea. That left ten of the seventeen types with a question BODY nobody
 * could write without hand-authoring JSON — a diagram's hotspots, a table's
 * cells, a set of notes with blanks in them.
 *
 * Two families cover seven of those widgets, and the whole reason they are
 * tractable is that both shapes are regular:
 *
 *   **A numbered slot list** — `hotspot_slot_list`, `label_slot_list`,
 *   `paragraph_slot_builder`. An array of `{key, <one field>}`, where `key` is
 *   `s1…sN`. The author writes the one field; the keys are ours to assign.
 *
 *   **Text carrying blank markers** — `note_builder`, `flowchart_builder`,
 *   `form_builder`, `table_builder`. Lines of text containing `{{s1}}`, plus a
 *   SEPARATE `slots` array listing which markers exist.
 *
 * That separate array is the interesting part. `payload_schema` requires it
 * alongside the text, so a hand-authoring teacher had to keep two things in
 * agreement: the markers they typed, and a list of the same names elsewhere. It
 * is derived here instead. There is one source of truth — the markers — and a
 * blank that exists in the text but not in the list is now impossible rather
 * than merely discouraged.
 *
 * Pure, for the reason the rest of this module is pure: these functions encode
 * what the SERVER will accept, and that is worth reading and testing without a
 * browser in the way. Nothing here validates — `payload_schema` is enforced
 * server-side against the registry, and a second copy of those rules in the
 * console is a second source of truth that drifts.
 */

/** A blank marker: `{{s1}}`, or the `{{1}}` people actually type. */
const MARKER = /\{\{\s*s?(\d+)\s*\}\}/g;
/** A cell or value that is NOTHING BUT a marker — that is what makes it a blank. */
const ONLY_MARKER = /^\s*\{\{\s*s?\d+\s*\}\}\s*$/;

/**
 * The slot ids a piece of text refers to, in the order they first appear.
 *
 * Order matters: it is the order a student meets the blanks, and the order the
 * answer-key grid numbers its rows. Sorting numerically instead would put
 * `{{s10}}` before `{{s2}}` for anybody who wrote the markers out of sequence.
 */
export function slotsInText(text: string): string[] {
  const seen: string[] = [];
  for (const match of text.matchAll(MARKER)) {
    const id = `s${Number(match[1])}`;
    if (!seen.includes(id)) seen.push(id);
  }
  return seen;
}

/** The slot ids across several lines, first appearance winning. */
export function slotsInLines(lines: readonly string[]): string[] {
  const seen: string[] = [];
  for (const line of lines) {
    for (const id of slotsInText(line)) if (!seen.includes(id)) seen.push(id);
  }
  return seen;
}

/** `{{1}}` and `{{ s1 }}` both become `{{s1}}`, which is what the schema shows. */
export function normaliseMarkers(text: string): string {
  return text.replace(MARKER, (_, digits) => `{{s${Number(digits)}}}`);
}

/** `s1…sN`, the ids every payload and key schema in the registry uses. */
export function keyFor(index: number): string {
  return `s${index + 1}`;
}

// ── a numbered slot list ─────────────────────────────────────────────

/**
 * `[{key: "s1", <field>: "…"}]` from what the author typed, one row each.
 *
 * Rows left empty are dropped rather than sent as a slot with a blank label:
 * `minItems` would accept it and the student would meet an unlabelled hotspot,
 * which is a question with no question in it.
 *
 * Keys are re-assigned from position on every edit, so deleting the second of
 * four rows renumbers the rest instead of leaving a gap at `s2`. A gap is legal
 * against the schema and wrong for a paper: the blanks a student sees are
 * numbered by where they are, not by what survived an edit.
 */
export function slotList(field: string, values: readonly string[]): object[] {
  return values
    .map((value) => value.trim())
    .filter(Boolean)
    .map((value, index) => ({ key: keyFor(index), [field]: value }));
}

/** Read an existing slot list back into the boxes. */
export function slotListValues(field: string, payload: unknown): string[] {
  const slots = (payload as { slots?: unknown } | null)?.slots;
  if (!Array.isArray(slots)) return [""];
  const values = slots
    .map((slot) => (slot && typeof slot === "object"
      ? String((slot as Record<string, unknown>)[field] ?? "") : ""));
  return values.length ? values : [""];
}

// ── one piece of prose with markers in it ────────────────────────────

/**
 * `{<field>: text, slots}` for a body that is a single passage of prose.
 *
 * `sentence_completion`, `summary_completion` and `summary_completion_bank` are
 * the three types shaped like this, and they were the three the console could
 * not produce a publishable question for. Their widget — `blank_editor` — had no
 * editor, so the body fell back to a JSON textarea; and `slots` is not on the
 * authoring form at ALL, so there was no box to type it into either. The console
 * could emit only `{"text": "…"}`, `POST /questions` took it (the payload is
 * checked at publish, not on write), and the gate then refused the whole paper
 * with `PAYLOAD_INVALID: 'slots' is a required property`.
 *
 * Deriving the array here is the fix, and it is the same fix the four text
 * builders above already use: the markers are the single source of truth, and a
 * blank that exists in the sentence but not in the list is now impossible.
 */
export function blankText(field: string, text: string): Record<string, unknown> {
  const body = text.trim();
  return { [field]: normaliseMarkers(body), slots: slotsInText(body) };
}

/** Read one back into the box, un-normalised — retyping is the author's. */
export function blankTextValue(field: string, payload: unknown): string {
  const value = (payload as Record<string, unknown> | null)?.[field];
  return typeof value === "string" ? value : "";
}

/**
 * The marker to insert next: one past the HIGHEST already in the text, not one
 * past the count.
 *
 * Deleting `{{s2}}` out of a three-blank sentence and inserting again must not
 * produce a second `{{s3}}` — two blanks sharing an id is one answer key entry
 * for two boxes, and the student's second answer would be marked against the
 * first one's accepted list.
 */
export function nextMarker(text: string): string {
  const used = slotsInText(text).map((slot) => Number(slot.slice(1)));
  return `{{s${used.length ? Math.max(...used) + 1 : 1}}}`;
}

/**
 * The blanks the BODY has already decided on, or null if it has not.
 *
 * Every builder in this module derives `slots` from what the author wrote, and
 * the answer key needs exactly one row per blank — so asking the author to press
 * "Add a blank" until the key agrees with a sentence they have already finished
 * is asking them to restate a decision. Worse, it is a decision they can restate
 * WRONG: a two-blank sentence with a one-row key is `KEY_SLOTS_MISSING` at
 * publish, which is a finding about a paper rather than a hint about a form.
 *
 * Two shapes are in the wild and both mean the same thing: `["s1","s2"]` from
 * the text builders, and `[{key: "s1", …}]` from the slot lists.
 *
 * `null`, not `[]`, when the payload declares nothing — the types whose key is
 * one choice with no `slots` at all must keep the manual control.
 */
export function slotIdsOf(payload: unknown): string[] | null {
  const slots = (payload as { slots?: unknown } | null)?.slots;
  if (!Array.isArray(slots) || slots.length === 0) return null;
  const ids = slots
    .map((slot) => (typeof slot === "string"
      ? slot
      : String((slot as Record<string, unknown> | null)?.["key"] ?? "")))
    .filter((id) => /^s[0-9]+$/.test(id));
  return ids.length ? ids : null;
}

// ── an audio cue ─────────────────────────────────────────────────────

/**
 * `audio_hint_ms` as a teacher reads it off a recording: `m:ss`.
 *
 * The schema wants milliseconds since the start of the track, which is not a
 * number anybody has. A bare number is taken as seconds, because someone typing
 * `90` into a box labelled with a time means a minute and a half, not a tenth of
 * a second.
 *
 * `undefined` for anything unparseable, and for empty — the field is optional,
 * and `0` is a different claim from "not set": it would mark the cue at the very
 * start of the track.
 */
export function parseTimestamp(text: string): number | undefined {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  const parts = trimmed.split(":");
  if (parts.length > 2) return undefined;
  if (parts.some((part) => !/^\d+$/.test(part.trim()))) return undefined;
  const seconds = parts.length === 2
    ? Number(parts[0]) * 60 + Number(parts[1])
    : Number(parts[0]);
  return Number.isFinite(seconds) ? Math.round(seconds * 1000) : undefined;
}

/** Back to `m:ss` for the box. */
export function formatTimestamp(ms: unknown): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "";
  const total = Math.round(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}


// ── structured text carrying markers ─────────────────────────────────

export type NoteKind = "heading" | "bullet" | "subbullet" | "line";

export type NoteBlock = { kind: NoteKind; text: string };

/**
 * `{blocks, slots}` for `note_completion`.
 *
 * `slots` is derived from the markers rather than asked for, so the two cannot
 * disagree.
 */
export function noteBlocks(blocks: readonly NoteBlock[]): Record<string, unknown> {
  const kept = blocks.filter((block) => block.text.trim());
  return {
    blocks: kept.map((block) => ({ kind: block.kind, text: normaliseMarkers(block.text.trim()) })),
    slots: slotsInLines(kept.map((block) => block.text)),
  };
}

export type FlowStep = { text: string; branch?: string };

/** `{steps, slots}` for `flowchart_completion`. */
export function flowSteps(steps: readonly FlowStep[]): Record<string, unknown> {
  const kept = steps.filter((step) => step.text.trim());
  return {
    steps: kept.map((step) => (step.branch?.trim()
      ? { text: normaliseMarkers(step.text.trim()), branch: step.branch.trim() }
      : { text: normaliseMarkers(step.text.trim()) })),
    slots: slotsInLines(kept.map((step) => step.text)),
  };
}

export type FormRow = { label: string; value: string };

/**
 * `{fields, slots}` for `form_completion`.
 *
 * A row whose value is a marker is the BLANK and carries `slot`; a row with
 * plain text is context the student reads and carries `value`. The schema has
 * both properties and forbids anything else, so the two cannot be conflated —
 * a blank sent as `value: "{{s1}}"` would render the marker to the student
 * instead of a box to type in.
 */
export function formFields(rows: readonly FormRow[]): Record<string, unknown> {
  const kept = rows.filter((row) => row.label.trim() || row.value.trim());
  return {
    fields: kept.map((row) => {
      const raw = row.value.trim();
      const label = row.label.trim();
      const slot = slotsInText(raw)[0];
      if (slot && ONLY_MARKER.test(raw)) return { label, slot };
      return raw ? { label, value: normaliseMarkers(raw) } : { label };
    }),
    slots: slotsInLines(kept.map((row) => row.value)),
  };
}

/**
 * `{columns, rows, slots}` for `table_completion`.
 *
 * A cell is `{kind: "text"|"blank", …}`; a cell whose text is exactly one
 * marker is the blank, anything else is a label. Deciding it from the CONTENT
 * rather than asking the author to pick a kind per cell is what keeps a table
 * editable in the time a teacher actually has.
 */
export function tableGrid(columns: readonly string[],
                          cells: readonly (readonly string[])[]): Record<string, unknown> {
  const keptColumns = columns.map((c) => c.trim()).filter(Boolean);
  const keptRows = cells.filter((row) => row.some((cell) => cell.trim()));
  return {
    columns: keptColumns,
    rows: keptRows.map((row) => row.slice(0, Math.max(1, keptColumns.length))
      .map((cell) => {
        const text = cell.trim();
        if (ONLY_MARKER.test(text)) {
          return { kind: "blank", slot: slotsInText(text)[0] ?? "s1" };
        }
        // `value`, not `text`. The cell schema forbids anything it does not
        // name, so the obvious guess is rejected outright by the server.
        return { kind: "text", value: normaliseMarkers(text) };
      })),
    slots: slotsInLines(keptRows.flat()),
  };
}
