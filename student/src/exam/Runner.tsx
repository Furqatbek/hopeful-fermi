/**
 * The exam runner.
 *
 * The chrome around it is the specified one — timer top-centre flashing at ten
 * and five minutes, palette along the bottom with Review turning a square into
 * a circle, passage left and questions right with a draggable divider
 * (`docs/design/0013` §1, §3).
 *
 * **The content is still a fixture.** Wiring it to a real attempt is the next
 * piece of work and it is not small: `POST /attempts`, then
 * `GET /attempts/{xid}/payload` for the questions, `POST
 * /attempts/{xid}/sections/{position}/enter` to start each section's server
 * clock, and the IndexedDB answer outbox flushing to `POST
 * /attempts/{xid}/answers` every 5-10 s and on blur — which §7.3 calls the
 * load-bearing endpoint of the whole product, because a dropped request there
 * must cost one round trip and not one answer.
 *
 * Kept honest rather than hidden: the banner below says so on screen, so nobody
 * mistakes this for a working mock.
 */

import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { BottomBar, ExamShell, TopBar } from "./Chrome";
import { Reading } from "./Reading";
import { sync, type Clock } from "./clock";
import { step, type Slot } from "./palette";
import { Settings, useDisplaySettings } from "../app/Settings";

const PASSAGE = `The Dead Sea, bordered by Jordan to the east and Israel and the West Bank to the west, lies 430 metres below sea level, making its shores the lowest dry land on Earth. Its water is roughly ten times saltier than ordinary seawater, a concentration that no fish and almost no plant can survive — which is how it came by its name.

That same salinity is what makes it famous. A bather does not swim so much as float, held on the surface by the density of the water. Visitors have travelled to the shore for this sensation, and for the reputed properties of its black mud, since at least the reign of Herod the Great.

The sea is shrinking. The River Jordan, which once fed it almost entirely, is now diverted upstream for agriculture and drinking water, and the mineral works at the southern end evaporate great volumes for potash. The surface has dropped more than thirty metres in a century, and the retreating shoreline has left thousands of sinkholes where fresh groundwater dissolves buried salt layers.

Proposals to save it have been debated for decades. The most ambitious would carry water from the Red Sea through a pipeline of some 180 kilometres, generating hydroelectricity on the descent and desalinating part of the flow before discharging the remainder. Critics argue that mixing two chemically distinct bodies of water could turn the Dead Sea red with algae, or white with gypsum, and that nobody can say with confidence which.`;

/** 40 questions across 3 passages — the real shape of a Reading section. */
function fixtureSlots(): Slot[] {
  return Array.from({ length: 40 }, (_, i) => ({
    number: i + 1,
    part: Math.floor(i / 14) + 1,
    answered: i < 9,
    flagged: i === 4,
  }));
}

export function ExamRunner() {
  const { xid } = useParams<{ xid: string }>();
  const [slots, setSlots] = useState<Slot[]>(fixtureSlots);
  const [current, setCurrent] = useState(1);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const display = useDisplaySettings();

  // A real attempt takes this from `GET /attempts/{xid}`, which returns
  // `server_now` beside `expires_at` precisely so the countdown is a server
  // delta. The shape here is identical; only the source is synthetic.
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
          <p className="runner__notice">
            <strong>Not wired up yet.</strong> The chrome is the specified one,
            but the questions and the clock are a fixture — assignment{" "}
            <code>{xid}</code> is not being loaded. Nothing you do here is saved.
          </p>
          <h2>Question {current}</h2>
          <p className="muted">
            Answers land here once the runner reads{" "}
            <code>GET /attempts/&#123;xid&#125;/payload</code>. The passage on the
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
