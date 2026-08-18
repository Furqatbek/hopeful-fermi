/**
 * Status marks. **Never controls.**
 *
 * This console's rule is that every control is a WORD — "Where used", "Billing",
 * "Retire" — because a word says what it does and a glyph needs a legend. That
 * rule is not being softened: nothing here may be the only content of a button
 * or a link, and `tests/icons.test.ts` fails the build if one is.
 *
 * What a glyph is genuinely for is the other thing: SCANNING a column. A status
 * cell renders the word `processing`, `ready` or `failed`, and finding the one
 * failed row among twenty-five means reading twenty-five words. A shape beside
 * the word is read at a glance, and the word is still there for anyone whose
 * reading of the shape is a guess.
 *
 * So every icon here is:
 *
 *   * **additive** — it sits next to the label, never instead of it;
 *   * **`aria-hidden`** — a screen reader gets the word, and an icon that also
 *     announced itself would say everything twice;
 *   * **`currentColor`** — the meaning comes from the surrounding colour, so
 *     `--good` and `--danger` do the work they were declared for rather than
 *     each icon carrying its own palette;
 *   * **inline SVG** — no sprite fetch, no icon font, no third-party request.
 *     ADR-0001 §9.1 bans the last of those, and a font that renders a box when
 *     it fails to load is worse than no icon at all.
 */

export type IconName = "ok" | "working" | "failed" | "waiting" | "done";

/**
 * Five shapes, and the axis they divide is **"does this row need someone?"**
 * rather than good and bad.
 *
 * That axis is what a person is actually reading the column for, and it is the
 * one a good/bad split gets wrong at both ends. A `rejected` takedown is not a
 * failure — somebody looked and refused it, correctly. An `archived` version is
 * not a success. Both are simply over, and a column that paints the first red
 * and the second green is a column that lies twice.
 *
 * The distinction that earns the fifth shape is between the two kinds of
 * ending: `waiting` means a person still has to do something, `done` means
 * nobody does. In a queue that is the most useful thing a glyph can carry.
 */
const PATHS: Record<IconName, string> = {
  // A tick. Two strokes, because a single polyline at this size renders as a
  // smudge on a non-retina screen.
  ok: "M3.5 8.5 L6.5 11.5 L12.5 4.5",
  // Three quarters of a circle: the universal "in flight", and unlike a dot it
  // reads as unfinished when it is not spinning — which it is not, because a
  // table that polls every four seconds does not need a moving part per row.
  working: "M14 8a6 6 0 1 1-6-6",
  // An exclamation, drawn as a stroke and a dot rather than a glyph, so it
  // keeps its weight beside the text at any size.
  failed: "M8 3.5 L8 9 M8 12 L8 12.01",
  // A clock hand at ten past: waiting, without implying a duration.
  waiting: "M8 4.5 L8 8 L10.5 9.5",
  // A dash, which is already this console's mark for "nothing here" in an empty
  // cell. Ringed, so it is a state rather than a missing value.
  done: "M5 8 L11 8",
};

/** The circle every shape sits in, except the tick, which is the shape. */
const RINGED: Record<IconName, boolean> = {
  ok: false, working: false, failed: true, waiting: true, done: true,
};

export function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg
      className={`icon${className ? ` ${className}` : ""}`}
      viewBox="0 0 16 16"
      width="1em"
      height="1em"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      // The word beside it is the accessible name. An icon that announced
      // itself as well would read "ready ready".
      aria-hidden="true"
      focusable="false"
    >
      {RINGED[name] && <circle cx="8" cy="8" r="6" />}
      <path d={PATHS[name]} />
    </svg>
  );
}

/**
 * The status vocabulary, in one place.
 *
 * Every screen that shows a lifecycle word maps it here rather than choosing a
 * shape locally — otherwise `failed` is a cross on one screen and a warning
 * triangle on the next, which is exactly the "what does this mean" cost that
 * made this console word-only in the first place.
 *
 * **Transcribed from the CHECK constraints**, not from what the screens looked
 * like they showed. Every word below is one a column can actually render, and
 * the migrations are the list: `0002` through `0015` name each vocabulary in a
 * `CHECK (status IN (...))`. A word invented here would be a shape no row ever
 * gets; a word missed would be a row with a blank where its neighbours have a
 * mark, which reads as a rendering fault rather than as a state.
 *
 * One word almost meant two things. `ready` is terminal for an audio track and
 * mid-flow for a regrade job, where it means "the dry run finished, now decide".
 * It is `ok` for both, because in both cases the machine's part succeeded — what
 * a person still has to do is carried by the Apply button sitting next to it,
 * which is a better place for it than a glyph.
 *
 * An unknown status gets NO icon rather than a default one. A new state added
 * to the server should look unfamiliar on the screen until somebody decides
 * what it means; quietly giving it a tick is how a screen lies.
 */
const STATUS: Record<string, IconName> = {
  // Concluded, and it worked.
  active: "ok", ready: "ok", published: "ok", scored: "ok", completed: "ok",
  committed: "ok", validated: "ok", clean: "ok", sent: "ok", matched: "ok",
  paid: "ok", granted: "ok", upheld: "ok", actioned: "ok", final: "ok",

  // The machine is on it. Nobody need do anything but wait.
  uploading: "working", processing: "working", parsing: "working",
  committing: "working", running: "working", in_progress: "working",
  investigating: "working", reviewing: "working", matching: "working",
  booking: "working", grading: "working", live: "working", planning: "working",
  completing: "working", prefetched: "working", started: "working",

  // Went wrong, or is wrong. The only red in a status column.
  failed: "failed", error: "failed", quarantined: "failed", suspended: "failed",
  mismatched: "failed", aborted: "failed", expired: "failed",
  disqualified: "failed", no_show: "failed",

  // A person has to act. The reason a queue is a queue.
  draft: "waiting", pending: "waiting", awaiting_payment: "waiting",
  queued: "waiting", new: "waiting", triage: "waiting", in_review: "waiting",
  submitted: "waiting", issued: "waiting", invited: "waiting",
  received: "waiting", scheduled: "waiting", registration: "waiting",
  lobby: "waiting", registered: "waiting", waiting: "waiting",
  not_started: "waiting", counter_noticed: "waiting", open: "waiting",

  // Over, deliberately. Nothing here to do.
  archived: "done", left: "done", closed: "done", deprecated: "done",
  dismissed: "done", removed: "done", deleted: "done", cancelled: "done",
  refunded: "done", rejected: "done", withdrawn: "done", abandoned: "done",
  voided: "done", suppressed: "done",
};

export function iconForStatus(status: string | null | undefined): IconName | null {
  return STATUS[String(status ?? "").toLowerCase()] ?? null;
}

/**
 * A status cell: the shape, then the word.
 *
 * One component rather than two lines at each call site, so the ORDER cannot
 * drift — the shape leads because that is the thing being scanned, and a column
 * where some rows lead with the word is a column that is slower to read than
 * one with no icons at all.
 *
 * The word is rendered exactly as the server sent it, `not_started` and all.
 * Prettifying it here would put a second vocabulary between the screen and the
 * API, and the first time somebody greps the console for a status they read on
 * it, they would find nothing.
 */
export function Status({ value }: { value: string | null | undefined }) {
  const name = iconForStatus(value);
  return (
    <span className={`status${name ? ` status--${name}` : ""}`}>
      {name && <Icon name={name} />}
      {value ?? "—"}
    </span>
  );
}
