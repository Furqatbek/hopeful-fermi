/**
 * The question navigator along the bottom of the screen.
 *
 * Verified behaviour from the real computer-delivered client
 * (`docs/design/0013-student-exam-ui.md` §1.2, §9):
 *
 *   * the bar sits at the BOTTOM and shows **all 40 questions** of the section;
 *   * the **Review** control is at the LOWER LEFT;
 *   * flagging a question changes its marker **from a square to a circle**.
 *
 * That square→circle change is the part worth being pedantic about. It is the
 * real visual language of the exam, a student who has drilled on it reads the
 * bar at a glance under time pressure, and a product that substitutes a colour
 * or a little flag icon has quietly taught a dialect the test does not speak.
 *
 * Pure functions, no React. The marker rules are the kind of thing that is
 * obviously right until an edge case (flagged AND answered AND current) renders
 * something nobody intended, so they are testable without a DOM.
 */

export type Slot = {
  /** 1-based, and continuous across parts: 1..40 for a whole section. */
  number: number;
  /** Which part of the section this question belongs to. Listening has 4. */
  part: number;
  answered: boolean;
  flagged: boolean;
};

/** Square unless flagged — see the note above. */
export type Shape = "square" | "circle";

export type Marker = {
  shape: Shape;
  /** Filled once answered, so progress is legible without reading numbers. */
  filled: boolean;
  current: boolean;
};

export function marker(slot: Slot, currentNumber: number): Marker {
  return {
    shape: slot.flagged ? "circle" : "square",
    filled: slot.answered,
    current: slot.number === currentNumber,
  };
}

/** Group the flat 1..40 run into its parts, preserving order. */
export function byPart(slots: readonly Slot[]): { part: number; slots: Slot[] }[] {
  const groups: { part: number; slots: Slot[] }[] = [];
  for (const slot of slots) {
    const last = groups[groups.length - 1];
    if (last && last.part === slot.part) last.slots.push(slot);
    else groups.push({ part: slot.part, slots: [slot] });
  }
  return groups;
}

/**
 * How many are still blank. Shown as plain text beside the palette, because
 * counting forty small shapes with eight minutes left is not a task to give a
 * person who is already under pressure.
 */
export function unanswered(slots: readonly Slot[]): number {
  return slots.reduce((count, slot) => count + (slot.answered ? 0 : 1), 0);
}

/**
 * Step to the next or previous question, CLAMPED rather than wrapping.
 *
 * Wrapping from 40 back to 1 is the kind of convenience that costs a student
 * their place: they press Next once too often at the end of the section and are
 * silently thrown back to the beginning, and on a timed exam the few seconds
 * spent working out what happened are real.
 */
export function step(slots: readonly Slot[], current: number, delta: -1 | 1): number {
  const index = slots.findIndex((slot) => slot.number === current);
  if (index === -1) return current;
  const next = index + delta;
  if (next < 0 || next >= slots.length) return current;
  return slots[next]!.number;
}
