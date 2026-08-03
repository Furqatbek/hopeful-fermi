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
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { api, problemText } from "../../api/client";
import {
  ACCEPT_ATTRIBUTE,
  type Attestation,
  type Progress,
  uploadAudioTrack,
} from "./upload";

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
  const [title, setTitle] = useState("");
  const [claim, setClaim] = useState<Attestation["claim"] | "">("");
  const [licenceNote, setLicenceNote] = useState("");
  const [progress, setProgress] = useState<Progress | null>(null);
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
      );
    },
    onSuccess: () => {
      setTitle("");
      setClaim("");
      setLicenceNote("");
      if (fileInput.current) fileInput.current.value = "";
      void queries.invalidateQueries({ queryKey: ["audio-tracks"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  return (
    <div className="page">
      <h1>Listening audio</h1>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
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

        <button disabled={upload.isPending}>
          {upload.isPending ? "Uploading…" : "Upload"}
        </button>
      </form>

      {progress && <p className="muted">{describe(progress)}</p>}
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
              <th>Transcript</th>
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
                <td className="muted">{track.has_transcript ? "yes" : "—"}</td>
              </tr>
            ))}
            {tracks.data.items?.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No audio yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
