/**
 * The listening player.
 *
 * The server has had play-once grants, delivery-asset resolution, signed URLs
 * and range streaming since phase 8. This is the half that was missing, and
 * three properties of the server's design dictate its shape.
 *
 * ── 1. the grant is minted on the student's click, never on mount ───────────
 *
 * In exam mode `POST .../audio-grant` succeeds exactly once; a second call
 * answers `409 audio_already_played`. So spending it because a component
 * rendered would burn the single play on somebody who opened the section to
 * read the questions first. Nothing here touches the network until the student
 * presses a button that says what it is about to do.
 *
 * ── 2. the whole track is fetched ONCE, into memory, before it plays ────────
 *
 * A grant lives ~120 seconds. A listening section is thirty minutes of audio.
 * Handing the grant URL straight to an `<audio src>` would work for about two
 * minutes and then fail: browsers issue *several* range requests across
 * playback, and every one after the TTL is a 403 — with no way to re-mint,
 * because the play is already spent. The section would die mid-sentence and be
 * unrecoverable.
 *
 * A single `fetch()` does not have that problem. The grant is checked when the
 * request STARTS; the response then streams for as long as it takes, TTL
 * notwithstanding. So one request, held open, read to a Blob, played from an
 * object URL. Playback is then immune to the network entirely — which on a
 * Tashkent connection is worth more than the memory it costs, and it also means
 * one grant really does mean one delivery.
 *
 * ── 3. the audio is never given a seek bar in exam mode ─────────────────────
 *
 * The real test plays the recording once, start to finish, with no transport.
 * `controls` would hand back scrubbing, which is a different exam.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { API_PREFIX, problemText } from "../api/client";
import * as attempt from "./attempt";

type Phase =
  | { name: "idle" }
  | { name: "loading"; got: number; total: number }
  | { name: "ready" }
  | { name: "spent" }
  | { name: "error"; message: string };

export type AudioProps = {
  attemptXid: string;
  position: number;
  mode: "exam" | "practice";
  /** 0-100, owned by the Runner so the top bar's slider and this agree. */
  volume: number;
  /** Fired when the track reaches its end, so the Runner can start the
   *  post-listening window without polling the element. */
  onEnded?: () => void;
};

/** Bytes → an object URL, reporting progress so a long fetch is not a dead UI. */
async function download(
  url: string, onProgress: (got: number, total: number) => void,
): Promise<string> {
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok) {
    throw new Error(
      response.status === 403
        ? "That recording link has expired. Tell your invigilator."
        : `The recording could not be loaded (${response.status}).`);
  }
  const total = Number(response.headers.get("content-length") ?? 0);
  // `body` is absent on very old browsers; fall back to a plain blob rather than
  // failing, because a progress bar is a nicety and the audio is not.
  if (!response.body) return URL.createObjectURL(await response.blob());

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      chunks.push(value);
      got += value.length;
      onProgress(got, total);
    }
  }
  return URL.createObjectURL(new Blob(chunks));
}

export function AudioSection({
  attemptXid, position, mode, volume, onEnded,
}: AudioProps) {
  const [phase, setPhase] = useState<Phase>({ name: "idle" });
  // The browser refused to start on its own; the student must press play.
  const [needsGesture, setNeedsGesture] = useState(false);
  const element = useRef<HTMLAudioElement | null>(null);
  const objectUrl = useRef<string | null>(null);
  // Guards the grant against a double-click and against React 18 double-invoking
  // an effect in development. Minting twice in exam mode is unrecoverable.
  const minting = useRef(false);

  useEffect(() => () => {
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
  }, []);

  useEffect(() => {
    if (element.current) element.current.volume = Math.min(1, Math.max(0, volume / 100));
  }, [volume, phase]);

  const play = useCallback(async () => {
    if (minting.current) return;
    minting.current = true;
    setPhase({ name: "loading", got: 0, total: 0 });
    try {
      const granted = await attempt.audioGrant(attemptXid, position) as {
        grant: string; media_xid: string; plays_remaining: number | null;
      };
      const url = `${API_PREFIX}/media/${granted.media_xid}/content`
        + `?grant=${encodeURIComponent(granted.grant)}`;
      const blob = await download(url, (got, total) =>
        setPhase({ name: "loading", got, total }));
      objectUrl.current = blob;
      setPhase({ name: "ready" });
      // Try to start on our own, and be ready to be refused.
      //
      // The student's click is several seconds and two awaits behind us by now,
      // and a browser's autoplay policy wants `play()` inside the gesture's own
      // task. Chrome refuses after a gap unless the origin has media engagement,
      // so on a student's first listening section this WILL be blocked — and
      // exam mode renders no `controls`, which would leave them looking at a
      // loaded recording with no way to start it and a clock running.
      //
      // So: attempt it, and on rejection show a play button. That button's
      // click calls `play()` synchronously with the blob already in memory, so
      // it cannot be refused and starts instantly.
      setTimeout(() => {
        void element.current?.play()
          .then(() => setNeedsGesture(false))
          .catch(() => setNeedsGesture(true));
      }, 0);
    } catch (failure) {
      setPhase({ name: "error", message: problemText(failure) });
    } finally {
      // Deliberately NOT reset in exam mode: the play is spent whether or not
      // the download succeeded, and offering the button again would promise a
      // second grant the server will refuse.
      if (mode === "practice") minting.current = false;
    }
  }, [attemptXid, position, mode]);

  if (phase.name === "idle") {
    return (
      <div className="audio audio--idle">
        <button type="button" className="audio__start" onClick={() => void play()}>
          Play the recording
        </button>
        {mode === "exam"
          ? (
            <p className="audio__warning">
              You will hear this <b>once</b>. It cannot be paused, rewound or
              played again. Check your volume before you start.
            </p>
          )
          : <p className="audio__hint">Practice mode — you can replay this as often as you like.</p>}
      </div>
    );
  }

  if (phase.name === "loading") {
    const pct = phase.total ? Math.round((phase.got / phase.total) * 100) : null;
    return (
      <div className="audio audio--loading">
        <p className="audio__status" aria-live="polite">
          Loading the recording{pct === null ? "…" : ` — ${pct}%`}
        </p>
        <div className="audio__bar" role="progressbar"
             aria-valuenow={pct ?? 0} aria-valuemin={0} aria-valuemax={100}>
          <i style={{ width: `${pct ?? 8}%` }} />
        </div>
        <p className="audio__hint">
          The whole recording is downloaded before it starts, so a dropped
          connection cannot interrupt it once it is playing.
        </p>
      </div>
    );
  }

  if (phase.name === "error") {
    return (
      <div className="audio audio--error">
        <p className="error">{phase.message}</p>
        {mode === "practice" && (
          <button type="button" className="audio__start" onClick={() => void play()}>
            Try again
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="audio audio--playing">
      <audio
        ref={element}
        src={objectUrl.current ?? undefined}
        // No `controls` in exam mode: the real test gives no transport, and a
        // seek bar is a different exam. Practice gets the full set.
        controls={mode === "practice"}
        onPlay={() => setNeedsGesture(false)}
        onEnded={() => { setPhase({ name: "spent" }); onEnded?.(); }}
      />
      {needsGesture && phase.name !== "spent" && (
        <>
          <button
            type="button"
            className="audio__start"
            onClick={() => { void element.current?.play().catch(() => undefined); }}
          >
            Start playing
          </button>
          <p className="audio__hint">
            The recording is downloaded and ready. Your browser needs one more
            tap before it will play sound.
          </p>
        </>
      )}
      {mode === "exam" && !needsGesture && (
        <p className="audio__status" aria-live="polite">
          {phase.name === "spent"
            ? "The recording has finished."
            : "Playing. Answer as you listen."}
        </p>
      )}
    </div>
  );
}
