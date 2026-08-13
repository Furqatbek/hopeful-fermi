/**
 * The student app shell.
 *
 * Sign-in and the real screens land next; what is wired here is the exam
 * runner's chrome, because that is the part `docs/design/0013` specifies against
 * the real client and the part everything else is arranged around.
 *
 * The `/exam/demo` route renders the shell with synthetic data so the layout,
 * the timer thresholds and the square→circle palette can be looked at without a
 * database, a published test, or a seeded attempt. It is a fixture, not a
 * feature: §6's warning about practice affordances leaking into exam mode
 * applies to it too, so it is excluded from the production build below.
 */

import { useEffect, useState } from "react";

import { ExamShell, BottomBar, TopBar } from "../exam/Chrome";
import { sync, type Clock } from "../exam/clock";
import { step, type Slot } from "../exam/palette";
import { Settings, useDisplaySettings } from "./Settings";

/** 40 questions across 4 parts, which is the real shape of a Listening section. */
function demoSlots(): Slot[] {
  return Array.from({ length: 40 }, (_, i) => ({
    number: i + 1,
    part: Math.floor(i / 10) + 1,
    answered: i < 12,
    flagged: i === 5 || i === 17,
  }));
}

function ExamDemo() {
  const [slots, setSlots] = useState<Slot[]>(demoSlots);
  const [current, setCurrent] = useState(1);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [volume, setVolume] = useState(80);
  const display = useDisplaySettings();

  // A real attempt takes this from `GET /attempts/{xid}`, which returns
  // `server_now` beside `expires_at` precisely so the countdown is a server
  // delta. The shape here is identical; only the source is synthetic.
  const [clock] = useState<Clock>(() => {
    const now = new Date();
    return sync({
      serverNow: now.toISOString(),
      expiresAt: new Date(now.getTime() + 11 * 60_000).toISOString(),
    });
  });

  // Arrow keys move between questions. A candidate whose mouse dies mid-exam
  // must still be able to finish (§5).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "ArrowRight") setCurrent((c) => step(slots, c, 1));
      if (e.key === "ArrowLeft") setCurrent((c) => step(slots, c, -1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [slots]);

  function toggleReview() {
    setSlots((all) =>
      all.map((s) => (s.number === current ? { ...s, flagged: !s.flagged } : s)));
  }

  return (
    <>
      <ExamShell
        top={
          <TopBar
            candidate="Aziza K."
            section="Listening · Part 1"
            clock={clock}
            onSettings={() => setSettingsOpen(true)}
            volume={{ value: volume, onChange: setVolume }}
          />
        }
        bottom={
          <BottomBar
            slots={slots}
            current={current}
            onGo={setCurrent}
            onToggleReview={toggleReview}
          />
        }
      >
        <div style={{ padding: "1.5rem", overflowY: "auto", height: "100%" }}>
          <h1 style={{ marginTop: 0 }}>Question {current}</h1>
          <p style={{ color: "var(--muted)", maxWidth: "42rem" }}>
            The chrome around this panel is the specified one: timer top-centre,
            flashing at ten minutes and again at five; volume and Settings upper
            right; the palette along the bottom with all forty questions, Review
            at the lower left turning the marker from a square into a circle.
          </p>
          <p style={{ color: "var(--muted)" }}>
            Wait for the clock to cross 10:00 and 5:00 to see both warning states,
            press Review to flag question {current}, and use ← → to move.
          </p>
        </div>
      </ExamShell>

      <div className="too-small">
        <h1>This needs a larger screen</h1>
        <p>
          Reading and Writing are sat side by side with the passage, exactly as
          the real computer-delivered test presents them. On a phone that becomes
          scrolling back and forth, which trains a skill the exam does not test.
        </p>
        <p>Open this on a laptop or tablet.</p>
      </div>

      {settingsOpen && <Settings {...display} onClose={() => setSettingsOpen(false)} />}
    </>
  );
}

export function App() {
  return <ExamDemo />;
}
