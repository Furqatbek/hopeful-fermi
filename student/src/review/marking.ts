/**
 * Turning a flat list of marked slots into something a student can read.
 *
 * `GET /attempts/{xid}/review` returns one item PER SLOT, in no particular
 * order, identified by `(question_version_xid, slot_key)` and a display
 * `number`. That is the right shape for a marking record and the wrong shape for
 * a screen: a five-gap table completion is five items, and a student wants one
 * question with five gaps in it, sitting under the section it came from.
 *
 * The joining is here rather than in the component because it is the part that
 * can be wrong in a way nobody notices — a question matched to the wrong
 * section, or an item silently dropped because the paper does not mention it —
 * and none of that is visible by looking at the rendered page.
 */

/** One marked slot, as the server reports it. */
export type Item = {
  number?: number;
  question_version_xid: string;
  slot_key: string;
  verdict: string;
  awarded: number;
  max_points: number;
  raw_response?: string | null;
  normalized_response?: string | null;
  accepted_answers?: string[];
  matched_alternative?: string | null;
  explain?: Record<string, unknown>;
  transcript_excerpt?: string | null;
};

export type Question = {
  number: number;
  question_version_xid: string;
  type_key: string;
  payload: Record<string, unknown>;
  slot_keys?: string[];
};

export type Section = {
  position: number;
  skill: string;
  title?: string;
  passage?: { title?: string } | null;
  groups?: { questions?: Question[] }[];
};

export type MarkedQuestion = {
  question: Question | null;
  /** The version xid, which is the only identifier every item carries. */
  xid: string;
  number: number;
  items: Item[];
  awarded: number;
  max: number;
};

export type MarkedSection = {
  position: number;
  title: string;
  skill: string;
  questions: MarkedQuestion[];
  awarded: number;
  max: number;
};

export type Tally = { awarded: number; max: number; right: number; of: number };

/** Sum a set of slots. Kept separate because it is used at three levels. */
export function tally(items: readonly Item[]): Tally {
  let awarded = 0;
  let max = 0;
  let right = 0;
  for (const item of items) {
    awarded += item.awarded;
    max += item.max_points;
    // Whole slots, not points: "18 of 20" is the sentence a student says, and a
    // partially credited slot is not one of the eighteen.
    if (item.verdict === "correct") right += 1;
  }
  return { awarded, max, right, of: items.length };
}

/**
 * Group the marked slots under the paper's own sections and questions.
 *
 * Items the paper does not mention still appear, under a trailing section — an
 * item that scored and cannot be placed is exactly the thing that must not be
 * silently dropped, and it happens for real: a question whose key was missing
 * when the paper was published produces marks with no `number`.
 */
export function organise(sections: readonly Section[], items: readonly Item[]): MarkedSection[] {
  const byXid = new Map<string, Item[]>();
  for (const item of items) {
    const held = byXid.get(item.question_version_xid);
    if (held) held.push(item);
    else byXid.set(item.question_version_xid, [item]);
  }

  const placed = new Set<string>();
  const out: MarkedSection[] = [];

  for (const section of sections) {
    const questions: MarkedQuestion[] = [];
    for (const group of section.groups ?? []) {
      for (const question of group.questions ?? []) {
        const own = byXid.get(question.question_version_xid);
        if (!own) continue;
        placed.add(question.question_version_xid);
        questions.push({
          question,
          xid: question.question_version_xid,
          number: question.number,
          items: [...own].sort(bySlot),
          ...totals(own),
        });
      }
    }
    if (!questions.length) continue;
    questions.sort((a, b) => a.number - b.number);
    out.push({
      position: section.position,
      title: section.title ?? section.passage?.title ?? `Part ${section.position}`,
      skill: section.skill,
      questions,
      ...totals(questions.flatMap((q) => q.items)),
    });
  }

  const orphans = [...byXid.entries()].filter(([xid]) => !placed.has(xid));
  if (orphans.length) {
    const questions = orphans.map(([xid, own]) => ({
      question: null,
      xid,
      number: own[0]?.number ?? 0,
      items: [...own].sort(bySlot),
      ...totals(own),
    })).sort((a, b) => a.number - b.number);
    out.push({
      position: Number.MAX_SAFE_INTEGER,
      title: "Marked, but not on the paper",
      skill: "",
      questions,
      ...totals(questions.flatMap((q) => q.items)),
    });
  }

  return out;
}

function totals(items: readonly Item[]): { awarded: number; max: number } {
  const t = tally(items);
  return { awarded: t.awarded, max: t.max };
}

/** `s2` after `s10` is wrong; slots are named, so compare their numbers. */
function bySlot(a: Item, b: Item): number {
  const na = Number(a.slot_key.replace(/^\D+/, ""));
  const nb = Number(b.slot_key.replace(/^\D+/, ""));
  if (Number.isFinite(na) && Number.isFinite(nb) && na !== nb) return na - nb;
  return a.slot_key.localeCompare(b.slot_key);
}

/**
 * What to say about one marked slot, in a student's words.
 *
 * `explain` carries the normaliser chain and is the honest answer to "why was
 * this wrong", but a list of internal normaliser names is not an answer anybody
 * can use. The one case worth translating is the near miss: the answer matched
 * an accepted alternative after normalising, or differs from one only by the
 * things the marker forgives. Everything else says plainly what was expected.
 */
export function nearMiss(item: Item): string | null {
  if (item.verdict === "correct") return null;
  // A set answer is a list of option letters. "One character away from B" over
  // a choice of B and C is not a spelling hint, it is noise — and it fires
  // easily, because single letters are one edit from each other.
  if (isSelection(item)) return null;
  const written = (item.raw_response ?? "").trim();
  if (!written) return null;
  const normalised = (item.normalized_response ?? "").trim();
  const accepted = item.accepted_answers ?? [];
  if (!accepted.length) return null;

  const folded = normalised.toLowerCase();
  const close = accepted.find((a) => {
    const other = a.trim().toLowerCase();
    return other !== folded && (other.replace(/\s+/g, "") === folded.replace(/\s+/g, "")
      || levenshtein(other, folded) <= 1);
  });
  if (!close) return null;
  return `One character away from “${close}”.`;
}

/** Small and bounded: only ever called on two short answers. */
function levenshtein(a: string, b: string): number {
  if (Math.abs(a.length - b.length) > 1) return 99;
  const rows = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array<number>(b.length).fill(0)]);
  for (let j = 0; j <= b.length; j += 1) rows[0]![j] = j;
  for (let i = 1; i <= a.length; i += 1) {
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      rows[i]![j] = Math.min(rows[i - 1]![j]! + 1, rows[i]![j - 1]! + 1, rows[i - 1]![j - 1]! + cost);
    }
  }
  return rows[a.length]![b.length]!;
}


/** Was this slot marked as a SET of choices rather than one value? */
export function isSelection(item: Item): boolean {
  return (item.explain as { primitive?: string } | undefined)?.primitive === "set_selection";
}

/**
 * The student's own answers, keyed by slot, in the shape the question renderer
 * wants — so review can show the paper exactly as it was sat.
 *
 * A set answer arrives as the scorer's display string, `", ".join(sorted(...))`,
 * because that is what `raw_response` is for. Splitting it back is reversing one
 * known join rather than parsing something arbitrary, and without it a
 * multi-select renders with NOTHING ticked under the words "You wrote B, C" —
 * which tells a student two contradictory things about their own answer.
 */
export function answersFor(entry: MarkedQuestion): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  for (const item of entry.items) {
    const written = item.raw_response ?? "";
    if (!isSelection(item)) {
      out[item.slot_key] = written;
      continue;
    }
    // **A set answer is reported under the slot key `"selection"`**, which is
    // the scorer's own name for "the whole item" and matches no slot the
    // question declares. Renderers key on the question's slot, so keying on
    // what review sends puts the answer somewhere nothing looks — the boxes
    // came back untouched, directly beneath the words "You wrote B, C".
    const slot = entry.question?.slot_keys?.[0] ?? item.slot_key;
    out[slot] = written.split(",").map((s) => s.trim()).filter(Boolean);
  }
  return out;
}
