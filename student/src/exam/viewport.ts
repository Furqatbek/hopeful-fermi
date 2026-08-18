/**
 * Whether this device may sit the exam at all.
 *
 * The refusal below 1024px is a product decision, not a limitation (0013 §8.1):
 * Reading is sat with the passage beside the questions, and on a phone that
 * becomes scrolling back and forth, which trains a skill the real test does not
 * examine.
 *
 * **It was enforced in CSS alone**, and CSS hides things rather than stopping
 * them. `.exam { display: none }` left the runner mounted and running its whole
 * lifecycle behind a blank screen: `POST /attempts` started the attempt, the
 * clock ran, autosave flushed — and `POST .../audio-grant` spent the ONE play a
 * listening section allows, server-side and unrecoverable. A student who opened
 * a paper on a phone to see what it looked like burned their single play and
 * their attempt without ever seeing a question.
 *
 * So the gate is a mount decision now, and this module is the rule behind it,
 * kept pure so it can be tested without a browser.
 */

/** The breakpoint, in the one place it is written. Matches `styles.css`. */
export const EXAM_MIN_WIDTH = "(min-width: 1024px)";

/**
 * The gate LATCHES OPEN and never closes.
 *
 * A student mid-paper who resizes a window, rotates a tablet, or opens dev
 * tools must not have the runner unmounted under them — that would drop the
 * section they are in and re-enter it, and re-entering is not free when the
 * audio grant is already spent. The refusal exists to stop an exam STARTING on
 * the wrong device, which is a question asked once.
 *
 * The other direction is worth keeping: someone who opened a narrow window and
 * then maximised it gets the exam, rather than being told to reload.
 */
export function widen(previous: boolean, matches: boolean): boolean {
  return previous || matches;
}

/**
 * The viewport right now, or `true` where the question cannot be asked.
 *
 * A browser with no `matchMedia` is old enough to be a genuine problem, but
 * refusing a student their exam over a missing feature-detection API is a worse
 * failure than letting a narrow screen through — the CSS refusal still applies,
 * and a human can see the result.
 */
export function wideEnough(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return true;
  }
  return window.matchMedia(EXAM_MIN_WIDTH).matches;
}
