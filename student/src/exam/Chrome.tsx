/**
 * The exam chrome: the fixed top bar and the fixed bottom question palette.
 *
 * Every position here is verified against the real computer-delivered client
 * and is not a design preference — see `docs/design/0013-student-exam-ui.md` §1
 * and its sources. Specifically:
 *
 *   * the timer is TOP CENTRE, and flashes at ten minutes and again at five;
 *   * volume and settings are UPPER RIGHT;
 *   * the question palette is along the BOTTOM and shows all 40 of the section;
 *   * Review is LOWER LEFT and turns the marker from a square into a circle.
 *
 * The shell never scrolls. Only the body between these two bars does, and it
 * scrolls per pane. A page that scrolls as a whole loses the fixed timer and
 * fixed palette, which is most of what makes the screen feel like the exam.
 */

import { useEffect, useState } from "react";

import { format, remaining, urgency, type Clock } from "./clock";
import { byPart, marker, unanswered, type Slot } from "./palette";

export function Timer({ clock }: { clock: Clock }) {
  const [ms, setMs] = useState(() => remaining(clock));

  useEffect(() => {
    setMs(remaining(clock));
    // 250ms, not 1000ms. A one-second interval drifts against the real second
    // boundary, so the display visibly skips a number every so often — which on
    // an exam clock reads as the timer glitching at the worst possible moment.
    const tick = setInterval(() => setMs(remaining(clock)), 250);
    return () => clearInterval(tick);
  }, [clock]);

  const state = urgency(ms);
  return (
    <div
      className={`exam-timer exam-timer--${state}`}
      // Announced politely, and only when it changes meaningfully: a live region
      // that fires four times a second would make a screen reader unusable.
      role="timer"
      aria-live={state === "normal" ? "off" : "polite"}
    >
      <span className="exam-timer__value">{format(ms)}</span>
      <span className="exam-timer__unit">left</span>
    </div>
  );
}

export function TopBar({
  candidate, clock, section, onSettings, volume,
}: {
  candidate: string;
  clock: Clock;
  section: string;
  onSettings: () => void;
  /** Listening only. The real client puts a volume bar in the upper right. */
  volume?: { value: number; onChange: (v: number) => void };
}) {
  return (
    <header className="exam-top">
      <div className="exam-top__left">
        <span className="exam-top__candidate">{candidate}</span>
        <span className="exam-top__section">{section}</span>
      </div>

      {/* Centre. Deliberately unavoidable — that is why the real test puts it here. */}
      <div className="exam-top__centre">
        <Timer clock={clock} />
      </div>

      <div className="exam-top__right">
        {volume && (
          <label className="exam-volume">
            <span className="exam-volume__label">Volume</span>
            <input
              type="range" min={0} max={100} value={volume.value}
              onChange={(e) => volume.onChange(Number(e.target.value))}
              aria-label="Volume"
            />
          </label>
        )}
        <button type="button" className="exam-top__button" onClick={onSettings}>
          Settings
        </button>
      </div>
    </header>
  );
}

export function BottomBar({
  slots, current, onGo, onToggleReview, onFinish, finishing,
}: {
  slots: readonly Slot[];
  current: number;
  onGo: (n: number) => void;
  onToggleReview: () => void;
  onFinish: () => void;
  finishing: boolean;
}) {
  const flagged = slots.find((s) => s.number === current)?.flagged ?? false;

  return (
    <footer className="exam-bottom">
      {/* Lower left, as in the real client. */}
      <button
        type="button"
        className="exam-review"
        aria-pressed={flagged}
        onClick={onToggleReview}
      >
        Review
      </button>

      <nav className="exam-palette" aria-label="Questions">
        {byPart(slots).map((group) => (
          <div key={group.part} className="exam-palette__part">
            <span className="exam-palette__part-label">Part {group.part}</span>
            <ul className="exam-palette__slots">
              {group.slots.map((slot) => {
                const m = marker(slot, current);
                return (
                  <li key={slot.number}>
                    <button
                      type="button"
                      onClick={() => onGo(slot.number)}
                      aria-current={m.current ? "true" : undefined}
                      aria-label={
                        `Question ${slot.number}` +
                        `${slot.answered ? ", answered" : ", not answered"}` +
                        `${slot.flagged ? ", flagged for review" : ""}`
                      }
                      className={[
                        "exam-slot",
                        `exam-slot--${m.shape}`,
                        m.answered ? "exam-slot--answered" : "",
                        m.current ? "exam-slot--current" : "",
                      ].filter(Boolean).join(" ")}
                    >
                      {slot.number}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      {/* Counting forty small shapes with eight minutes left is not a task to
          hand somebody who is already under pressure. */}
      <p className="exam-bottom__count">{unanswered(slots)} left</p>

      {/* Ending the exam lives HERE, at the far end of the bar, and not beside
          the answer field where it used to be.

          It was one Tab away from the gap being typed into — and tabbing from
          one gap to the next is the ordinary way to fill these in, so the key
          that should move a student forward ended their exam instead, with
          Space or Enter and no confirmation. Distance in the tab order is the
          fix; `onFinish` opening a confirmation is the belt to that braces. */}
      <button
        type="button"
        className="exam-finish"
        onClick={onFinish}
        disabled={finishing}
      >
        {finishing ? "Submitting…" : "Finish"}
      </button>
    </footer>
  );
}

/** The non-scrolling three-region shell every section sits inside. */
export function ExamShell({
  top, children, bottom,
}: {
  top: React.ReactNode;
  children: React.ReactNode;
  bottom: React.ReactNode;
}) {
  return (
    <div className="exam">
      {top}
      <main className="exam-body">{children}</main>
      {bottom}
    </div>
  );
}
