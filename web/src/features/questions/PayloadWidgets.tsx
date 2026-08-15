/**
 * The eight body editors the registry declares and `TypeForm` fell back to JSON
 * for.
 *
 * Ten of the seventeen types could not have their question WRITTEN without
 * hand-authoring JSON. All ten are covered here.
 *
 * Three shapes, and the regularity is what makes this tractable at all:
 *
 *   **A numbered slot list** — the author writes one field per row and the keys
 *   are assigned from position. Nobody types `s1`.
 *
 *   **Text carrying `{{s1}}` markers** — the author writes the line as the
 *   student will read it, and the `slots` array the schema demands alongside is
 *   DERIVED from the markers rather than typed a second time. Two lists that
 *   have to agree is a bug waiting; one list and a function is not.
 *
 *   **One passage of prose with markers in it** — the same derivation over a
 *   single field. `blank_editor`, added last and the one that mattered most: the
 *   three types using it could not reach a published paper at all, because the
 *   JSON fallback covered the text and nothing on the form could produce the
 *   `slots` array beside it. See `blankText`.
 *
 * Everything shape-related lives in `payloadParts.ts` and is tested without a
 * browser. What is here is the arrangement of boxes.
 */

import { useRef, useState } from "react";

import {
  type FlowStep, type FormRow, type NoteBlock, type NoteKind,
  blankText, blankTextValue, flowSteps, formFields, nextMarker, noteBlocks,
  slotList, slotListValues, slotsInLines, slotsInText, tableGrid,
} from "./payloadParts";

export type Payload = Record<string, unknown>;

/** Widgets rendered here. `TypeForm` keeps its own list for the simple ones. */
export const COMPOSITE = new Set([
  "hotspot_slot_list", "label_slot_list", "paragraph_slot_builder",
  "note_builder", "flowchart_builder", "form_builder", "table_builder",
  "blank_editor",
]);

/** The single field a slot-list widget collects, and how to ask for it. */
const SLOT_FIELD: Record<string, { field: string; label: string; hint: string;
                                   placeholder: string }> = {
  label_slot_list: {
    field: "label", label: "Label",
    hint: "One row per thing the student has to find on the map or plan.",
    placeholder: "the ticket office",
  },
  hotspot_slot_list: {
    field: "hint", label: "Hint",
    hint: "One row per labelled point on the diagram. The hint is optional — "
      + "it is what the student sees beside the box, not the answer.",
    placeholder: "the part that turns",
  },
  paragraph_slot_builder: {
    field: "paragraph", label: "Paragraph",
    hint: "One row per paragraph the student matches a heading to, in the order "
      + "they appear. A single capital letter each.",
    placeholder: "A",
  },
};

export function CompositeField({ spec, payload, onPatch }: {
  spec: {
    field: string; widget: string;
    label?: Record<string, string> | undefined;
    hint?: Record<string, string> | undefined;
    multiline?: boolean | undefined;
  };
  payload: Payload;
  onPatch: (next: Payload) => void;
}) {
  if (SLOT_FIELD[spec.widget]) {
    return <SlotList widget={spec.widget} field={spec.field}
                     payload={payload} onPatch={onPatch} />;
  }
  if (spec.widget === "blank_editor") {
    return <BlankTextBuilder spec={spec} payload={payload} onPatch={onPatch} />;
  }
  if (spec.widget === "note_builder") {
    return <NoteBuilder field={spec.field} payload={payload} onPatch={onPatch} />;
  }
  if (spec.widget === "flowchart_builder") {
    return <FlowBuilder field={spec.field} payload={payload} onPatch={onPatch} />;
  }
  if (spec.widget === "form_builder") {
    return <FormBuilder field={spec.field} payload={payload} onPatch={onPatch} />;
  }
  return <TableBuilder field={spec.field} payload={payload} onPatch={onPatch} />;
}

/** How many blanks the markers add up to, shown so the author can check it
 *  against the answer key without counting by eye. */
function SlotCount({ slots }: { slots: string[] }) {
  return (
    <p className="muted">
      {slots.length === 0
        ? "No blanks yet — write {{1}} where the student types."
        : `${slots.length} blank${slots.length === 1 ? "" : "s"}: ${slots.join(", ")}. `
          + "The answer key below needs one row for each."}
    </p>
  );
}

/**
 * One passage of prose, with the blanks marked in it.
 *
 * The whole body of a sentence completion or a summary. It was a JSON textarea,
 * and — worse — the `slots` array its schema also requires had no field on the
 * form at all, so nothing an author could type here produced a question that
 * would publish. `blankText` derives that array from the markers.
 *
 * The marker is INSERTED, not typed. `{{s1}}` is syntax, and asking a teacher to
 * remember it is the same request in a smaller box: the button and Ctrl+B put
 * the next one at the caret, which is where the blank goes — a sentence's gap is
 * almost never at the end of it.
 */
function BlankTextBuilder({ spec, payload, onPatch }: {
  spec: {
    field: string;
    label?: Record<string, string> | undefined;
    hint?: Record<string, string> | undefined;
    multiline?: boolean | undefined;
  };
  payload: Payload;
  onPatch: (next: Payload) => void;
}) {
  // The draft, for the same reason every builder here holds one: `blankText`
  // trims and normalises, so round-tripping through the payload would rewrite
  // the box under the author's cursor as they typed.
  const [text, setText] = useState(() => blankTextValue(spec.field, payload));
  const box = useRef<HTMLTextAreaElement>(null);
  const id = `f-${spec.field}`;

  const write = (next: string) => {
    setText(next);
    onPatch(blankText(spec.field, next));
  };

  const insertBlank = () => {
    const marker = nextMarker(text);
    const element = box.current;
    // No element, or a browser that gives no selection: append. Losing the
    // caret should cost the author a drag, not the blank.
    const at = element?.selectionStart ?? text.length;
    const to = element?.selectionEnd ?? text.length;
    write(`${text.slice(0, at)}${marker}${text.slice(to)}`);
    // After React has written the new value, or the caret jumps to the end.
    requestAnimationFrame(() => {
      element?.focus();
      element?.setSelectionRange(at + marker.length, at + marker.length);
    });
  };

  return (
    <>
      <label htmlFor={id}>{spec.label?.["en"] ?? spec.field.replaceAll("_", " ")}</label>
      <p className="muted">
        {spec.hint?.["en"]
          ?? "Write it as the student will read it, and put a blank where they type."}
      </p>
      <textarea
        id={id}
        ref={box}
        rows={spec.multiline ? 8 : 3}
        value={text}
        placeholder="The bridge opened in {{s1}}."
        onChange={(event) => write(event.target.value)}
        onKeyDown={(event) => {
          if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "b") {
            event.preventDefault();
            insertBlank();
          }
        }}
      />
      <div className="row">
        <button type="button" className="link" onClick={insertBlank}>
          Insert blank (Ctrl+B)
        </button>
      </div>
      <SlotCount slots={slotsInText(text)} />
    </>
  );
}

function Rows({ children, onAdd, onRemove, addLabel }: {
  children: React.ReactNode;
  onAdd: () => void;
  /** `| undefined` explicitly, because `exactOptionalPropertyTypes` is on and
   *  "not passed" and "passed as undefined" are different types under it. The
   *  callers compute this conditionally, so they pass it either way. */
  onRemove?: (() => void) | undefined;
  addLabel: string;
}) {
  return (
    <>
      {children}
      <div className="row">
        <button type="button" className="link" onClick={onAdd}>{addLabel}</button>
        {onRemove && (
          <button type="button" className="link" onClick={onRemove}>
            Remove the last one
          </button>
        )}
      </div>
    </>
  );
}

function SlotList({ widget, field, payload, onPatch }: {
  widget: string; field: string; payload: Payload; onPatch: (next: Payload) => void;
}) {
  const spec = SLOT_FIELD[widget]!;
  // The DRAFT lives here, not in the payload. `slotList` drops empty rows —
  // correct, because an unlabelled hotspot is a question with no question in
  // it — so a payload round trip would delete the blank row the instant
  // "Add a row" created it, and the button would do nothing at all.
  const [values, setValues] = useState<string[]>(() => slotListValues(spec.field, payload));
  const write = (next: string[]) => {
    setValues(next);
    onPatch({ [field]: slotList(spec.field, next) });
  };

  return (
    <>
      <h3>{spec.label}s</h3>
      <p className="muted">{spec.hint}</p>
      <Rows
        addLabel="Add a row"
        onAdd={() => write([...values, ""])}
        onRemove={values.length > 1 ? () => write(values.slice(0, -1)) : undefined}
      >
        {values.map((value, index) => (
          <span key={index} className="key-row">
            <label htmlFor={`f-slot-${index}`}>{index + 1}</label>
            <input
              id={`f-slot-${index}`}
              value={value}
              placeholder={spec.placeholder}
              onChange={(e) => {
                const next = [...values];
                next[index] = e.target.value;
                write(next);
              }}
            />
          </span>
        ))}
      </Rows>
    </>
  );
}

const NOTE_KINDS: NoteKind[] = ["heading", "bullet", "subbullet", "line"];

function NoteBuilder({ field, payload, onPatch }: {
  field: string; payload: Payload; onPatch: (next: Payload) => void;
}) {
  const [blocks, setBlocks] = useState<NoteBlock[]>(() =>
    (Array.isArray(payload[field]) && (payload[field] as []).length
      ? (payload[field] as NoteBlock[]) : [{ kind: "heading", text: "" }]));
  // Only the BUILT value is patched. Writing the draft under the same key
  // would overwrite `blocks` with un-normalised text and undo the derivation.
  const write = (next: NoteBlock[]) => { setBlocks(next); onPatch(noteBlocks(next)); };

  return (
    <>
      <h3>The notes</h3>
      <p className="muted">
        Write each line as the student will read it, and put <code>{"{{1}}"}</code>,{" "}
        <code>{"{{2}}"}</code> where they fill a blank in.
      </p>
      <Rows
        addLabel="Add a line"
        onAdd={() => write([...blocks, { kind: "bullet", text: "" }])}
        onRemove={blocks.length > 1 ? () => write(blocks.slice(0, -1)) : undefined}
      >
        {blocks.map((block, index) => (
          <span key={index} className="key-row">
            <select
              aria-label={`Line ${index + 1} kind`}
              value={block.kind}
              onChange={(e) => {
                const next = [...blocks];
                next[index] = { ...block, kind: e.target.value as NoteKind };
                write(next);
              }}
            >
              {NOTE_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <input
              aria-label={`Line ${index + 1}`}
              value={block.text}
              placeholder="Opened in {{1}}"
              onChange={(e) => {
                const next = [...blocks];
                next[index] = { ...block, text: e.target.value };
                write(next);
              }}
            />
          </span>
        ))}
      </Rows>
      <SlotCount slots={slotsInLines(blocks.map((b) => b.text))} />
    </>
  );
}

function FlowBuilder({ field, payload, onPatch }: {
  field: string; payload: Payload; onPatch: (next: Payload) => void;
}) {
  const [steps, setSteps] = useState<FlowStep[]>(() =>
    (Array.isArray(payload[field]) && (payload[field] as []).length
      ? (payload[field] as FlowStep[]) : [{ text: "" }, { text: "" }]));
  const write = (next: FlowStep[]) => { setSteps(next); onPatch(flowSteps(next)); };

  return (
    <>
      <h3>The steps</h3>
      <p className="muted">
        In order, top to bottom. <code>{"{{1}}"}</code> marks a blank. A branch
        label is the word on the arrow into the step — leave it empty for a
        straight line.
      </p>
      <Rows
        addLabel="Add a step"
        onAdd={() => write([...steps, { text: "" }])}
        onRemove={steps.length > 2 ? () => write(steps.slice(0, -1)) : undefined}
      >
        {steps.map((step, index) => (
          <span key={index} className="key-row">
            <label htmlFor={`f-step-${index}`}>{index + 1}</label>
            <input
              id={`f-step-${index}`}
              value={step.text}
              placeholder="Collect the {{1}}"
              onChange={(e) => {
                const next = [...steps];
                next[index] = { ...step, text: e.target.value };
                write(next);
              }}
            />
            <input
              aria-label={`Step ${index + 1} branch`}
              value={step.branch ?? ""}
              placeholder="branch (optional)"
              onChange={(e) => {
                const next = [...steps];
                next[index] = { ...step, branch: e.target.value };
                write(next);
              }}
            />
          </span>
        ))}
      </Rows>
      <SlotCount slots={slotsInLines(steps.map((s) => s.text))} />
    </>
  );
}

function FormBuilder({ field, payload, onPatch }: {
  field: string; payload: Payload; onPatch: (next: Payload) => void;
}) {
  const [rows, setRows] = useState<FormRow[]>(() =>
    (Array.isArray(payload[field]) && (payload[field] as []).length
      ? (payload[field] as FormRow[]) : [{ label: "", value: "" }]));
  const write = (next: FormRow[]) => { setRows(next); onPatch(formFields(next)); };

  return (
    <>
      <h3>The form</h3>
      <p className="muted">
        One row per line of the form. Put <code>{"{{1}}"}</code> in the value to
        make it a blank; type real text to leave it filled in as an example.
      </p>
      <Rows
        addLabel="Add a line"
        onAdd={() => write([...rows, { label: "", value: "" }])}
        onRemove={rows.length > 1 ? () => write(rows.slice(0, -1)) : undefined}
      >
        {rows.map((row, index) => (
          <span key={index} className="key-row">
            <input
              aria-label={`Line ${index + 1} label`}
              value={row.label}
              placeholder="Surname"
              onChange={(e) => {
                const next = [...rows];
                next[index] = { ...row, label: e.target.value };
                write(next);
              }}
            />
            <input
              aria-label={`Line ${index + 1} value`}
              value={row.value}
              placeholder="{{1}}"
              onChange={(e) => {
                const next = [...rows];
                next[index] = { ...row, value: e.target.value };
                write(next);
              }}
            />
          </span>
        ))}
      </Rows>
      <SlotCount slots={slotsInLines(rows.map((r) => r.value))} />
    </>
  );
}

type Grid = { columns: string[]; cells: string[][] };

function FormGridDefaults(payload: Payload, field: string): Grid {
  const columns = Array.isArray(payload["columns"])
    ? (payload["columns"] as string[]) : ["", ""];
  const cells = Array.isArray(payload[field]) && (payload[field] as []).length
    ? (payload[field] as string[][])
    : [columns.map(() => "")];
  return { columns, cells };
}

function TableBuilder({ field, payload, onPatch }: {
  field: string; payload: Payload; onPatch: (next: Payload) => void;
}) {
  const start = FormGridDefaults(payload, field);
  const [columns, setColumns] = useState<string[]>(start.columns);
  const [cells, setCells] = useState<string[][]>(start.cells);
  // `tableGrid` alone. Patching the raw grid under `field` as well would
  // overwrite the built `rows` with arrays of plain strings, and the server
  // would reject every table — the draft belongs here, not in the payload.
  const write = (nextColumns: string[], nextCells: string[][]) => {
    setColumns(nextColumns);
    setCells(nextCells);
    onPatch(tableGrid(nextColumns, nextCells));
  };

  return (
    <>
      <h3>The table</h3>
      <p className="muted">
        Name the columns, then fill the cells. A cell that is exactly{" "}
        <code>{"{{1}}"}</code> becomes a blank; anything else is text the student
        reads.
      </p>
      <div className="row">
        {columns.map((column, c) => (
          <input
            key={c}
            aria-label={`Column ${c + 1}`}
            value={column}
            placeholder={`Column ${c + 1}`}
            onChange={(e) => {
              const next = [...columns];
              next[c] = e.target.value;
              write(next, cells);
            }}
          />
        ))}
        <button type="button" className="link"
                onClick={() => write([...columns, ""],
                                     cells.map((r) => [...r, ""]))}>
          Add a column
        </button>
      </div>
      {cells.map((row, r) => (
        <span key={r} className="key-row">
          <label>{r + 1}</label>
          {columns.map((_, c) => (
            <input
              key={c}
              aria-label={`Row ${r + 1} column ${c + 1}`}
              value={row[c] ?? ""}
              placeholder={c === columns.length - 1 ? "{{1}}" : "text"}
              onChange={(e) => {
                const next = cells.map((line) => [...line]);
                next[r]![c] = e.target.value;
                write(columns, next);
              }}
            />
          ))}
        </span>
      ))}
      <div className="row">
        <button type="button" className="link"
                onClick={() => write(columns, [...cells, columns.map(() => "")])}>
          Add a row
        </button>
        {cells.length > 1 && (
          <button type="button" className="link"
                  onClick={() => write(columns, cells.slice(0, -1))}>
            Remove the last row
          </button>
        )}
      </div>
      <SlotCount slots={slotsInLines(cells.flat())} />
    </>
  );
}
