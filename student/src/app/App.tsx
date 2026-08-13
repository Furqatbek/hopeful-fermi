/**
 * The student app shell.
 *
 * Signed out it shows sign-in; signed in it shows the exam runner. The runner
 * is currently driven by a fixture rather than a real attempt — wiring it to
 * `POST /attempts` and `GET /attempts/{xid}/payload` is the next step — but
 * every piece of chrome around it is the specified one, so what a student sees
 * is already the layout `docs/design/0013` describes.
 *
 * `isSignedIn` is a hint, not an authority: it cannot read the httpOnly refresh
 * cookie. A wrong `true` costs one 401 and a bounce back here, which is the same
 * path an expired session already takes.
 */

import { useEffect, useState } from "react";

import { isSignedIn } from "../api/session";
import { SignIn } from "../auth/SignIn";
import { BottomBar, ExamShell, TopBar } from "../exam/Chrome";
import { Reading } from "../exam/Reading";
import { sync, type Clock } from "../exam/clock";
import { step, type Slot } from "../exam/palette";
import { Settings, useDisplaySettings } from "./Settings";

const PASSAGE = `The Dead Sea, bordered by Jordan to the east and Israel and the West Bank to the west, lies 430 metres below sea level, making its shores the lowest dry land on Earth. Its water is roughly ten times saltier than ordinary seawater, a concentration that no fish and almost no plant can survive — which is how it came by its name.

That same salinity is what makes it famous. A bather does not swim so much as float, held on the surface by the density of the water. Visitors have travelled to the shore for this sensation, and for the reputed properties of its black mud, since at least the reign of Herod the Great.

The sea is shrinking. The River Jordan, which once fed it almost entirely, is now diverted upstream for agriculture and drinking water, and the mineral works at the southern end evaporate great volumes for potash. The surface has dropped more than thirty metres in a century, and the retreating shoreline has left thousands of sinkholes where fresh groundwater dissolves buried salt layers.

Proposals to save it have been debated for decades. The most ambitious would carry water from the Red Sea through a pipeline of some 180 kilometres, generating hydroelectricity on the descent and desalinating part of the flow before discharging the remainder. Critics argue that mixing two chemically distinct bodies of water could turn the Dead Sea red with algae, or white with gypsum, and that nobody can say with confidence which.`;

/** 40 questions across 4 parts — the real shape of a section. */
function fixtureSlots(): Slot[] {
  return Array.from({ length: 40 }, (_, i) => ({
    number: i + 1,
    part: Math.floor(i / 14) + 1,
    answered: i < 9,
    flagged: i === 4,
  }));
}

function ExamRunner() {
  const [slots, setSlots] = useState<Slot[]>(fixtureSlots);
  const [current, setCurrent] = useState(1);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const display = useDisplaySettings();

  // A real attempt takes this from `GET /attempts/{xid}`, which returns
  // `server_now` beside `expires_at` precisely so the countdown is a server
  // delta. Identical shape here; only the source is synthetic.
  const [clock] = useState<Clock>(() => {
    const now = new Date();
    return sync({
      serverNow: now.toISOString(),
      expiresAt: new Date(now.getTime() + 60 * 60_000).toISOString(),
    });
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "ArrowRight") setCurrent((c) => step(slots, c, 1));
      if (e.key === "ArrowLeft") setCurrent((c) => step(slots, c, -1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [slots]);

  return (
    <>
      <ExamShell
        top={
          <TopBar
            candidate="Aziza K."
            section="Reading · Passage 1"
            clock={clock}
            onSettings={() => setSettingsOpen(true)}
          />
        }
        bottom={
          <BottomBar
            slots={slots}
            current={current}
            onGo={setCurrent}
            onToggleReview={() =>
              setSlots((all) => all.map((s) =>
                s.number === current ? { ...s, flagged: !s.flagged } : s))}
          />
        }
      >
        <Reading title="The Dead Sea" passage={PASSAGE}>
          <h2>Question {current}</h2>
          <p className="muted">
            Answer inputs land here when the runner is wired to
            <code> GET /attempts/&#123;xid&#125;/payload</code>. The passage on the
            left is live: select text and right-click to highlight it, right-click
            a highlight to clear it, and drag the divider to rebalance the panes.
          </p>
        </Reading>
      </ExamShell>

      <div className="too-small">
        <h1>This needs a larger screen</h1>
        <p>
          Reading is sat with the passage beside the questions, exactly as the
          real computer-delivered test presents it. On a phone that becomes
          scrolling back and forth, which trains a skill the exam does not test.
        </p>
        <p>Open this on a laptop or tablet.</p>
      </div>

      {settingsOpen && <Settings {...display} onClose={() => setSettingsOpen(false)} />}
    </>
  );
}

export function App() {
  const [signedIn, setSignedIn] = useState(isSignedIn);

  // The API client dispatches this when a refresh fails: the session is over and
  // no retry helps. Listening here rather than importing a router into
  // `client.ts` keeps the API layer testable without a DOM.
  useEffect(() => {
    const ended = () => setSignedIn(false);
    window.addEventListener("ielts:signed-out", ended);
    return () => window.removeEventListener("ielts:signed-out", ended);
  }, []);

  if (!signedIn) return <SignIn onSignedIn={() => setSignedIn(true)} />;
  return <ExamRunner />;
}
