/**
 * The exam runner, wired to a real attempt.
 *
 * The order is the contract's and every step depends on the one before it:
 * start the attempt, fetch the frozen paper, enter the section (which starts
 * that section's server clock), answer into the outbox, flush, submit.
 *
 * ── what is deliberate here ────────────────────────────────────────────────
 *
 *   * **A keystroke reaches IndexedDB before it reaches the network.** The
 *     outbox is the record and React state is the rendering. A tab crash or a
 *     dropped connection costs nothing.
 *   * **The clock is re-anchored on every flush**, because the answers response
 *     carries `server_now` and `expires_at`. The countdown therefore corrects
 *     itself every few seconds without a socket and without trusting the device.
 *   * **Submit is not refused when the timer hits zero.** There is a 30-second
 *     grace window and the server records the overrun; refusing client-side at
 *     +1s would throw away an exam the server would have accepted.
 *   * **The audio grant is minted on the student's click**, never on load. In
 *     exam mode it succeeds exactly once, so spending it because a component
 *     mounted would burn the single play on somebody reading ahead.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { problemText } from "../api/client";
import { Settings, useDisplaySettings } from "../app/Settings";
import { BottomBar, ExamShell, TopBar } from "./Chrome";
import { AudioSection } from "./Audio";
import { Reading } from "./Reading";
import { QuestionView, type Answers, type Group } from "./Question";
import { remaining, sync, type Clock } from "./clock";
import { step, type Slot } from "./palette";
import * as attempt from "./attempt";
import * as outbox from "./outbox";
import { EXAM_MIN_WIDTH, widen, wideEnough } from "./viewport";

/** Flush cadence. The contract asks for every few seconds and on every screen
 *  change; 7 s sits inside the 5-10 s it names and keeps the radio mostly idle. */
const FLUSH_MS = 7_000;

/**
 * Has this slot been answered?
 *
 * A multi-select's value is a LIST, and an empty list is not an answer — but
 * `[] !== ""` is true, so the old string-only test would have lit the palette
 * marker for every unanswered multi-select on the paper.
 */
export function answered(value: string | string[] | undefined): boolean {
  return Array.isArray(value) ? value.length > 0 : (value ?? "") !== "";
}

type Payload = Awaited<ReturnType<typeof attempt.payload>>;

export function ExamRunner() {
  const { xid: assignmentXid } = useParams<{ xid: string }>();
  const navigate = useNavigate();
  const display = useDisplaySettings();

  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState<attempt.Started | null>(null);
  const [paper, setPaper] = useState<Payload | null>(null);
  const [clock, setClock] = useState<Clock | null>(null);
  const [sectionIndex, setSectionIndex] = useState(0);
  const [current, setCurrent] = useState(1);
  const [answers, setAnswers] = useState<Answers>({});
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [refused, setRefused] = useState(false);
  // The batch currently being delivered, so a retry carries the SAME key.
  const inFlight = useRef<outbox.InFlight | null>(null);
  // Whether this device may sit the paper. A MOUNT decision, not a stylesheet:
  // see `viewport.ts` for the play this used to burn behind a hidden screen.
  const [wide, setWide] = useState(wideEnough);
  // Owned here so the top bar's slider and the element agree, and so the
  // setting survives moving between sections.
  const [volume, setVolume] = useState(80);
  const [flagged, setFlagged] = useState<Set<number>>(new Set());
  const [submitting, setSubmitting] = useState(false);

  // Per-slot sequence numbers, and the keys for the two irreversible calls.
  // Refs, not state: changing them must never re-render, and a re-render must
  // never change them — a regenerated idempotency key is the same as none.
  const seqs = useRef<Record<string, number>>({});
  const startKey = useRef(attempt.idempotencyKey());
  const submitKey = useRef(attempt.idempotencyKey());
  const entered = useRef<Set<number>>(new Set());

  // Latches open only. A student mid-paper who resizes must not have the runner
  // unmounted under them; one who maximises a narrow window should get the exam.
  useEffect(() => {
    if (wide || typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(EXAM_MIN_WIDTH);
    const onChange = () => setWide((was) => widen(was, query.matches));
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [wide]);

  // ── start, then fetch the paper ──────────────────────────────────────────
  useEffect(() => {
    // `wide` gates the START, which is the call that cannot be taken back: it
    // creates the attempt, and everything mounted after it spends the audio
    // grant.
    if (!assignmentXid || !wide) return;
    let cancelled = false;
    void (async () => {
      try {
        // `POST /attempts` requires the mode and it must match the assignment's,
        // so the assignment is read first. Deep-linking straight to /exam/{xid}
        // has to work — a student who refreshes mid-paper arrives here with no
        // router state — so this is fetched rather than passed from Home.
        const mine = await attempt.assignment(assignmentXid);
        if (cancelled) return;
        if (!mine) { setError("That assignment is not assigned to you."); return; }
        const opened = await attempt.start(assignmentXid, mine.mode, startKey.current);
        if (cancelled) return;
        setStarted(opened);
        setClock(sync({ serverNow: opened.server_now, expiresAt: opened.expires_at }));
        const paper_ = await attempt.payload(opened.xid);
        if (cancelled) return;
        setPaper(paper_);

        // ── resume ────────────────────────────────────────────────────────
        // `POST /attempts` returns the attempt already in progress rather than
        // starting a second one, so arriving here after a refresh, a crash or a
        // closed lid is the SAME attempt — with answers on the server that this
        // screen knew nothing about.
        //
        // Seeding `seqs` is the half that is easy to miss and worse to omit. The
        // counters are per slot and start at zero on a fresh mount, while the
        // server discards any delta whose seq is not above the one it holds. A
        // client that resumed without this had every answer typed afterwards
        // rejected as `stale_seq` — invisibly, because a failed flush shows the
        // student nothing on purpose.
        const saved = await attempt.state(opened.xid);
        if (cancelled) return;
        const restored: Answers = {};
        for (const row of saved.answers ?? []) {
          const key = outbox.slotKey(row.question_version_xid, row.slot_key);
          seqs.current[key] = Math.max(seqs.current[key] ?? 0, row.client_seq ?? 0);
          if (row.response === null || row.response === undefined) continue;
          restored[key] = Array.isArray(row.response)
            ? row.response.map(String)
            : String(row.response);
        }
        // Merged UNDER anything already typed: a slow resume must never
        // overwrite a keystroke the student has made since the paper appeared.
        if (Object.keys(restored).length) {
          setAnswers((held) => ({ ...restored, ...held }));
        }
      } catch (failure) {
        if (!cancelled) setError(problemText(failure));
      }
    })();
    return () => { cancelled = true; };
  }, [assignmentXid, wide]);

  const sections = useMemo(
    () => (paper?.sections ?? []) as {
      position: number; skill: string; title?: string;
      passage?: { title?: string; blocks?: { runs?: { v?: string }[] }[] } | null;
      audio?: { track_xid: string; duration_ms?: number; play_once?: boolean } | null;
      groups?: Group[];
    }[],
    [paper],
  );
  const section = sections[sectionIndex];

  // ── entering a section starts ITS clock, server-side ────────────────────
  useEffect(() => {
    if (!started || !section || entered.current.has(section.position)) return;
    entered.current.add(section.position);
    attempt.enter(started.xid, section.position).catch((f) => setError(problemText(f)));
  }, [started, section]);

  // ── the flush loop ───────────────────────────────────────────────────────
  const flushNow = useCallback(async () => {
    if (!started) return;
    try {
      const waiting = await outbox.pending(started.xid);
      if (!waiting.length) return;
      // Collapse only when there is a genuine backlog: everything dropped is
      // provably superseded (same slot, lower seq), and below a batch there is
      // nothing to gain.
      const rows = outbox.batch(
        waiting.length > outbox.MAX_BATCH ? outbox.collapse(waiting) : waiting);
      // One key per BATCH, held until the batch is delivered. Minting it at the
      // call site gave every retry a fresh UUID, so the header was sent and the
      // server could not recognise a retry as one.
      const ids = rows.map((r) => r.id!).filter((id) => id !== undefined);
      inFlight.current = outbox.flushKey(inFlight.current, ids, attempt.idempotencyKey);
      const sent = await attempt.flush(started.xid, rows, inFlight.current.key);
      inFlight.current = null;

      // Delete what the server took; MARK what it refused. Deleting a refusal
      // too meant the student's answer was gone from disk, gone from the
      // server, and present only in React state — which survives exactly until
      // the reload this module exists to survive.
      const { accepted, refused: declined } = outbox.partition(rows, sent.rejected);
      await outbox.forget(accepted);
      await outbox.refuse(declined);
      // The response IS the clock sync.
      setClock(sent.clock);
      // A REFUSED delta is not a dropped connection — the server received the
      // answer and declined to store it, so the student is typing into a void
      // and only this line will ever tell them. It stayed silent through the
      // whole resume bug: every answer after a refresh was rejected `stale_seq`
      // and the screen looked perfectly normal.
      if (declined.length) setRefused(true);
    } catch {
      // A failed flush is not an error the student can act on. The deltas stay
      // in IndexedDB and go again on the next tick — which is the entire point
      // of the outbox, and showing a banner here would make a two-second wifi
      // dropout look like data loss.
    }
  }, [started]);

  useEffect(() => {
    if (!started) return;
    const tick = setInterval(() => { void flushNow(); }, FLUSH_MS);
    const onHide = () => { void flushNow(); };
    // `visibilitychange` rather than `blur`: it fires when a phone is locked or
    // the tab is backgrounded, which is exactly when a session is most likely to
    // be killed without warning.
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("pagehide", onHide);
    return () => {
      clearInterval(tick);
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("pagehide", onHide);
      void flushNow();
    };
  }, [started, flushNow]);

  // ── answering ────────────────────────────────────────────────────────────
  const onAnswer = useCallback(
    (questionXid: string, slot: string, value: string | string[]) => {
    setAnswers((held) => ({ ...held, [`${questionXid}:${slot}`]: value }));
    if (!started) return;
    const key = outbox.slotKey(questionXid, slot);
    const seq = outbox.nextSeq(seqs.current, key);
    seqs.current[key] = seq;
    // Queued before anything else. If everything after this line fails, the
    // answer is still on disk.
    void outbox.queue(started.xid, {
      question_version_xid: questionXid,
      slot_key: slot,
      // The slot's VALUE. The delta names its own slot_key, so the server
      // assembles the response object — see outbox.ts.
      response: value,
      client_seq: seq,
    });
  }, [started]);

  // ── the palette ──────────────────────────────────────────────────────────
  const slots: Slot[] = useMemo(() => {
    const out: Slot[] = [];
    for (const s of sections) {
      for (const group of s.groups ?? []) {
        for (const q of group.questions ?? []) {
          out.push({
            number: q.number,
            part: s.position,
            answered: (q.slot_keys ?? []).some(
              (k) => answered(answers[`${q.question_version_xid}:${k}`])),
            flagged: flagged.has(q.number),
          });
        }
      }
    }
    return out;
  }, [sections, answers, flagged]);

  const questionOf = useCallback((n: number) => {
    for (const [i, s] of sections.entries()) {
      for (const group of s.groups ?? []) {
        for (const q of group.questions ?? []) {
          if (q.number === n) return { section: i, group, question: q };
        }
      }
    }
    return null;
  }, [sections]);

  const goTo = useCallback((n: number) => {
    const found = questionOf(n);
    if (!found) return;
    if (found.section !== sectionIndex) setSectionIndex(found.section);
    setCurrent(n);
  }, [questionOf, sectionIndex]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "ArrowRight") setCurrent((c) => step(slots, c, 1));
      if (e.key === "ArrowLeft") setCurrent((c) => step(slots, c, -1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [slots]);

  // ── submit ───────────────────────────────────────────────────────────────
  const doSubmit = useCallback(async () => {
    if (!started || submitting) return;
    setSubmitting(true);
    try {
      // Everything on disk goes first, or the last thing typed is never marked.
      await flushNow();
      await attempt.submit(started.xid, submitKey.current);
      await outbox.drop(started.xid);
      void navigate(`/result/${started.xid}`);
    } catch (failure) {
      setError(problemText(failure));
      setSubmitting(false);
    }
  }, [started, submitting, flushNow, navigate]);

  // Time runs out: submit rather than refuse. The server allows 30 seconds of
  // grace and records the overrun, so the worst outcome of trying is a marked
  // exam; the worst outcome of not trying is an unmarked one.
  useEffect(() => {
    if (!clock || !started || submitting) return;
    if (remaining(clock) > 0) return;
    void doSubmit();
  }, [clock, started, submitting, doSubmit]);

  // ── render ───────────────────────────────────────────────────────────────
  if (!wide) {
    return (
      <div className="too-small">
        <h1>This needs a larger screen</h1>
        <p>
          Reading is sat with the passage beside the questions, exactly as the
          real computer-delivered test presents it. On a phone that becomes
          scrolling back and forth, which trains a skill the exam does not test.
        </p>
        <p>Open this on a laptop or tablet.</p>
      </div>
    );
  }

  if (error && !paper) {
    return (
      <main className="page">
        <h1>This could not be started</h1>
        <p className="error">{error}</p>
        <button onClick={() => { void navigate("/"); }}>Back to your work</button>
      </main>
    );
  }

  if (!paper || !started || !clock || !section) {
    return <main className="page"><p className="muted">Opening the paper…</p></main>;
  }

  const found = questionOf(current);
  const passageText = (section.passage?.blocks ?? [])
    .map((b) => (b.runs ?? []).map((r) => r.v ?? "").join(""))
    .join("\n\n");

  const unanswered = slots.filter((s) => !s.answered).length;

  const questions = (
    <>
      {error && <p className="error">{error}</p>}
      {section.audio && (
        <AudioSection
          // Remounting per section is deliberate: the object URL, the phase and
          // the spent-grant guard are all per section, and carrying any of them
          // across would offer a second play of a different track.
          key={`audio-${section.position}`}
          attemptXid={started.xid}
          position={section.position}
          mode={started.mode}
          volume={volume}
        />
      )}
      {refused && (
        <p className="runner__notice" role="alert">
          Some answers were not saved. Check this page and type them again — if
          the message stays, tell your invigilator now rather than at the end.
        </p>
      )}
      {found && (
        <QuestionView
          question={found.question}
          group={found.group}
          answers={Object.fromEntries(
            Object.entries(answers)
              .filter(([k]) => k.startsWith(`${found.question.question_version_xid}:`))
              .map(([k, v]) => [k.split(":")[1]!, v]),
          )}
          onAnswer={(slot, value) =>
            onAnswer(found.question.question_version_xid, slot, value)}
        />
      )}
    </>
  );

  return (
    <>
      <ExamShell
        top={
          <TopBar
            candidate={`Attempt ${started.attempt_no}`}
            section={`${section.title ?? section.skill} · ${started.mode}`}
            clock={clock}
            onSettings={() => setSettingsOpen(true)}
            // Only on a listening section — the real client shows the bar only
            // where there is something to hear.
            {...(section.audio ? { volume: { value: volume, onChange: setVolume } } : {})}
          />
        }
        bottom={
          <BottomBar
            slots={slots}
            current={current}
            onGo={goTo}
            onToggleReview={() => setFlagged((held) => {
              const next = new Set(held);
              if (next.has(current)) next.delete(current); else next.add(current);
              return next;
            })}
            onFinish={() => setConfirming(true)}
            finishing={submitting}
          />
        }
      >
        {section.passage
          ? (
            <Reading title={section.passage.title ?? section.title ?? "Passage"} passage={passageText}>
              {questions}
            </Reading>
          )
          : <div className="runner__single">{questions}</div>}
      </ExamShell>

      {/* The one irreversible thing a student can do in this screen, so it says
          what is about to happen and how much is unfinished. The timer running
          out submits WITHOUT this — the server decides when time is up, and a
          modal must never be what stands between an expired exam and its
          marking. */}
      {confirming && (
        <div className="confirm" role="dialog" aria-modal="true"
             aria-labelledby="confirm-title">
          <div className="confirm__box">
            <h2 id="confirm-title">Finish and submit?</h2>
            <p>
              {unanswered === 0
                ? "Every question has an answer."
                : `${unanswered} question${unanswered === 1 ? " is" : "s are"} still blank.`}
              {" "}You cannot return to this paper once it is submitted.
            </p>
            <div className="confirm__actions">
              <button type="button" className="confirm__cancel" autoFocus
                      onClick={() => setConfirming(false)}>
                Keep working
              </button>
              <button type="button" className="confirm__go" disabled={submitting}
                      onClick={() => { setConfirming(false); void doSubmit(); }}>
                {submitting ? "Submitting…" : "Submit"}
              </button>
            </div>
          </div>
        </div>
      )}

      {settingsOpen && <Settings {...display} onClose={() => setSettingsOpen(false)} />}
    </>
  );
}
