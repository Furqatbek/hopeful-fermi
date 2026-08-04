/**
 * The audio library and the upload form.
 *
 * The copyright attestation is a required field on the upload, not a setting
 * somebody accepted once at signup, and this screen must not make it feel
 * otherwise. "Assume some centres WILL try to upload published Cambridge
 * papers, and design so that liability and evidence are handled" — the evidence
 * is this control: the claim, the statement version, its hash, the uploader and
 * their IP, recorded per file. A pre-ticked box would make all of that worthless,
 * so nothing here is pre-selected and the statement is shown rather than linked.
 *
 * **Listening back is part of authoring, and it was the one thing this screen
 * could not do.** A centre could upload a 400 MB wav, watch it transcode, read
 * its measured loudness — and never hear a second of it, because the only issuer
 * of a media grant lived inside an exam attempt. `POST /audio-tracks/{xid}/grant`
 * is that missing issuer, and "is this the right file, and is it audible" is the
 * question the whole transcode pipeline exists to let somebody answer.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, problemText } from "../../api/client";
import "../media/media.css";
import { type Issued, isStale, mediaUrl, playbackFailure } from "./player";
import {
  ACCEPT_ATTRIBUTE,
  type Attestation,
  type Progress,
  uploadAudioTrack,
} from "./upload";
import { TranscriptEditor } from "./TranscriptEditor";
import { ArchiveButton } from "../archive/ArchiveButton";

const STATEMENT_VERSION = "1";

const CLAIMS: { value: Attestation["claim"]; label: string }[] = [
  { value: "original", label: "We created this recording ourselves" },
  { value: "licensed", label: "We hold a licence covering this use" },
  { value: "public_domain", label: "It is in the public domain" },
  { value: "permitted_excerpt", label: "It is a permitted excerpt" },
];

function describe(progress: Progress): string {
  switch (progress.phase) {
    case "hashing":
      return "Checksumming…";
    case "creating":
      return "Opening upload…";
    case "uploading":
      return `Uploading ${Math.round((progress.sent / Math.max(progress.total, 1)) * 100)}%`;
    case "assembling":
      return "Assembling…";
    case "transcoding":
      // Named rather than a spinner: this step shells out to ffmpeg in a worker
      // and a 30-minute section genuinely takes a while. A teacher who thinks it
      // has hung will upload it again.
      return "Normalising loudness — this can take a minute";
    case "ready":
      return `Ready · ${progress.durationMs ? Math.round(progress.durationMs / 1000) : "?"}s · ${progress.loudnessLufs ?? "?"} LUFS`;
    case "failed":
      return progress.reason;
  }
}

export function AudioLibrary() {
  const queries = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  // Held in a ref rather than in state: cancelling must reach the in-flight
  // upload, and a re-render is not what makes that happen.
  const aborter = useRef<AbortController | null>(null);
  const [title, setTitle] = useState("");
  const [claim, setClaim] = useState<Attestation["claim"] | "">("");
  const [licenceNote, setLicenceNote] = useState("");
  const [transcribing, setTranscribing] =
    useState<{ xid: string; title: string } | null>(null);
  const [playing, setPlaying] = useState<{ xid: string; title: string } | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [cancelled, setCancelled] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tracks = useQuery({
    queryKey: ["audio-tracks"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/audio-tracks", {
        params: { query: { limit: 50 } },
      });
      if (failure) throw failure;
      return data;
    },
    // While something is transcoding the list is stale within seconds.
    refetchInterval: (query) =>
      query.state.data?.items?.some((t) => t.status === "processing") ? 4000 : false,
  });

  const upload = useMutation({
    mutationFn: async () => {
      const file = fileInput.current?.files?.[0];
      if (!file) throw new Error("Choose a file first.");
      if (!claim) throw new Error("Say where this recording came from.");
      const controller = new AbortController();
      aborter.current = controller;
      return uploadAudioTrack(
        file,
        {
          title: title.trim() || file.name,
          attestation: {
            claim,
            statement_version: STATEMENT_VERSION,
            ...(licenceNote.trim() ? { licence_note: licenceNote.trim() } : {}),
          },
        },
        setProgress,
        { signal: controller.signal },
      );
    },
    onSuccess: () => {
      setTitle("");
      setClaim("");
      setLicenceNote("");
      if (fileInput.current) fileInput.current.value = "";
      void queries.invalidateQueries({ queryKey: ["audio-tracks"] });
    },
    onError: (failure) => {
      // Cancelling is not a failure and must not be painted as one. The upload
      // function still raises — it has to, so nothing downstream believes a
      // half-sent file arrived — but the person who pressed Cancel already knows
      // what happened.
      if (aborter.current?.signal.aborted) {
        setCancelled(true);
        setProgress(null);
        return;
      }
      setError(problemText(failure) || String(failure));
    },
  });

  return (
    <div className="page">
      <h1>Listening audio</h1>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setCancelled(false);
          setProgress(null);
          upload.mutate();
        }}
      >
        <label htmlFor="file">Audio file</label>
        <input id="file" type="file" accept={ACCEPT_ATTRIBUTE} ref={fileInput} required />

        <label htmlFor="title">Title</label>
        <input
          id="title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Section 1 — Accommodation enquiry"
        />

        <fieldset>
          <legend>Where did this recording come from?</legend>
          {/* Required, never pre-selected, and the statement is shown in full.
              This is the record a rights holder's lawyers are answered with. */}
          {CLAIMS.map((option) => (
            <label key={option.value} className="choice">
              <input
                type="radio"
                name="claim"
                value={option.value}
                checked={claim === option.value}
                onChange={() => setClaim(option.value)}
                required
              />
              {option.label}
            </label>
          ))}
          <p className="muted">
            By uploading you confirm the statement above is true and that this
            centre holds the right to use this recording. This confirmation is
            recorded with your name, the time and this file.
          </p>
        </fieldset>

        {claim === "licensed" && (
          <>
            <label htmlFor="licence">Licence reference</label>
            <input
              id="licence"
              value={licenceNote}
              onChange={(event) => setLicenceNote(event.target.value)}
              placeholder="Publisher, agreement number, expiry"
            />
          </>
        )}

        <div className="row">
          <button disabled={upload.isPending}>
            {upload.isPending ? "Uploading…" : "Upload"}
          </button>
          {upload.isPending && (
            <button
              type="button"
              className="link"
              onClick={() => aborter.current?.abort()}
            >
              Cancel
            </button>
          )}
        </div>
      </form>

      {progress && <p className="muted">{describe(progress)}</p>}
      {cancelled && (
        <p className="muted">
          Upload cancelled. The part-uploaded file was discarded, so nothing is
          stored and nothing is charged for. The track stays in the list below
          marked <strong>processing</strong> — there is no endpoint to remove one,
          so ignore it or ask an administrator.
        </p>
      )}
      {error && <p className="error">{error}</p>}

      {tracks.isError && <p className="error">{problemText(tracks.error)}</p>}
      {tracks.data && (
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Status</th>
              <th>Length</th>
              <th>Loudness</th>
              <th>Listen</th>
              <th>Transcript</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {tracks.data.items?.map((track) => (
              <tr key={track.xid}>
                <td>{track.title}</td>
                <td>{track.status}</td>
                <td>
                  {track.duration_ms ? `${Math.round(track.duration_ms / 1000)}s` : "—"}
                </td>
                <td>{track.loudness_lufs ?? "—"}</td>
                <td>
                  {/* Offered while a track is still processing as well as when
                      it is ready: the grant falls back to the master upload, so
                      an author who suspects they picked the wrong file can hear
                      it without waiting out a transcode of a 30-minute wav. */}
                  <button
                    className="link"
                    onClick={() =>
                      setPlaying(
                        playing?.xid === track.xid
                          ? null
                          : { xid: track.xid, title: track.title ?? "Untitled" },
                      )
                    }
                  >
                    {playing?.xid === track.xid ? "Close" : "Play"}
                  </button>
                </td>
                <td>
                  {/* The transcript is what makes post-exam review of a
                      listening question say anything at all — without one the
                      review screen shows a timestamp and no words. So this is a
                      control, not a yes/no column. */}
                  <button
                    className="link"
                    onClick={() =>
                      setTranscribing(
                        transcribing?.xid === track.xid
                          ? null
                          : { xid: track.xid, title: track.title ?? "Untitled" },
                      )
                    }
                  >
                    {transcribing?.xid === track.xid
                      ? "Close"
                      : track.has_transcript
                        ? "Edit"
                        : "Add"}
                  </button>
                </td>
                <td>
                  <ArchiveButton endpoint="/audio-tracks/{xid}/archive"
                                 xid={track.xid ?? ""}
                                 invalidate={["audio-tracks"]} label="track" />
                </td>
              </tr>
            ))}
            {tracks.data.items?.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No audio yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {playing && (
        <TrackPlayer
          key={playing.xid}
          trackXid={playing.xid}
          title={playing.title}
          onClose={() => setPlaying(null)}
        />
      )}

      {transcribing && (
        <TranscriptEditor
          trackXid={transcribing.xid}
          title={transcribing.title}
          onClose={() => setTranscribing(null)}
        />
      )}
    </div>
  );
}


/**
 * Playing a track back through a short-TTL grant.
 *
 * `useMutation` and component state, deliberately not `useQuery`. A grant is
 * valid for about two minutes and the query cache is not: a cached one would be
 * handed back on a remount, or refetched into a component that is not asking to
 * play anything. Requesting it is an act, it is timed, and it belongs to the
 * moment somebody pressed Play.
 *
 * The expiry is the failure worth designing for. A grant that ran out while the
 * page sat open does not raise anything — the element simply refuses to load,
 * and the report that reaches support is "the audio does not work". So the
 * remaining lifetime is tracked, the control says plainly when it has gone, and
 * a new one is one press away.
 */
function TrackPlayer({ trackXid, title, onClose }: {
  trackXid: string;
  title: string;
  onClose: () => void;
}) {
  const [held, setHeld] = useState<{ issued: Issued; receivedAt: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [failed, setFailed] = useState<string | null>(null);

  const request = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/audio-tracks/{xid}/grant", {
        params: { path: { xid: trackXid } },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setFailed(null);
      setHeld({ issued: data, receivedAt: Date.now() });
    },
    onError: (failure) => setFailed(problemText(failure)),
  });

  // Fetched once, on open: pressing Play IS the request. Empty deps are honest
  // here because the caller keys this component on the track, so a different
  // track is a different component rather than a re-run of this effect.
  const asked = useRef(false);
  useEffect(() => {
    if (asked.current) return;
    asked.current = true;
    request.mutate();
  }, [request]);

  // Only to move the countdown along. Five seconds, because the thing being
  // watched is a two-minute window and a second-by-second timer would re-render
  // the page a hundred and twenty times to say the same thing.
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(tick);
  }, []);

  const stale = held ? isStale(held.issued, held.receivedAt, now) : false;

  return (
    <div className="issued">
      <div className="row">
        <h2>{title}</h2>
        <button className="link" onClick={onClose}>Close</button>
      </div>

      {request.isPending && !held && <p className="muted">Requesting playback…</p>}
      {failed && <p className="error">{failed}</p>}

      {held && (
        <>
          {/* Keyed on the grant so a replacement REMOUNTS the element. Assigning
              a new `src` to a live element is not reliably reloaded by every
              browser, and the symptom of getting that wrong is the one this
              whole control exists to avoid: a player that looks fresh and plays
              nothing. */}
          <audio
            key={held.issued.grant}
            className="player"
            controls
            preload="metadata"
            src={mediaUrl(held.issued)}
            onError={(event) =>
              setFailed(playbackFailure(event.currentTarget.error?.code, stale))
            }
          />
          <p className="muted">
            {stale
              ? "This playback link has expired. Ask for a new one to listen again."
              : "This link is personal to you and lasts about two minutes. It is not "
                + "a shareable address, and audio is never served from a fixed URL."}
          </p>
        </>
      )}

      <button onClick={() => request.mutate()} disabled={request.isPending}>
        {request.isPending
          ? "Requesting…"
          : held ? "New playback link" : "Try again"}
      </button>
      {held && (
        <p className="muted">
          A new link restarts the track from the beginning.
        </p>
      )}
    </div>
  );
}
