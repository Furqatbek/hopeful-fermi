/**
 * Adding a section to a draft version.
 *
 * A section references a passage version or an audio track by xid — **nothing is
 * copied**. That is the authoring model's central claim, and a form that made
 * you re-upload a recording per test would quietly refute it, so both pickers
 * read the existing library.
 *
 * `play_once` defaults to true and only appears for listening. It is an exam
 * integrity rule ("play-once semantics enforced server-side for exam mode"), not
 * a preference, and showing it on a reading section would suggest otherwise.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";

export function AddSection({ versionXid, nextPosition }: {
  versionXid: string;
  nextPosition: number;
}) {
  const queries = useQueryClient();
  const [open, setOpen] = useState(false);
  const [skill, setSkill] = useState<"reading" | "listening">("reading");
  const [title, setTitle] = useState("");
  const [minutes, setMinutes] = useState("");
  const [sourceXid, setSourceXid] = useState("");
  const [playOnce, setPlayOnce] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const passages = useQuery({
    queryKey: ["passages"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/passages", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: open && skill === "reading",
  });

  const tracks = useQuery({
    queryKey: ["audio-tracks"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/audio-tracks", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: open && skill === "listening",
  });

  const add = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/test-versions/{xid}/sections", {
        params: { path: { xid: versionXid } },
        body: {
          skill,
          title: title.trim(),
          position: nextPosition,
          ...(minutes ? { time_limit_seconds: Number(minutes) * 60 } : {}),
          ...(skill === "listening"
            ? { play_once: playOnce, ...(sourceXid ? { audio_track_xid: sourceXid } : {}) }
            : sourceXid
              ? { passage_version_xid: sourceXid }
              : {}),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setOpen(false);
      setTitle("");
      setSourceXid("");
      setMinutes("");
      void queries.invalidateQueries({ queryKey: ["test-version", versionXid] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  if (!open) {
    return (
      <button className="link" onClick={() => setOpen(true)}>
        + Add a section
      </button>
    );
  }

  // Listening tracks that are not `ready` are deliberately excluded rather than
  // shown disabled: a section pointing at audio still in ffmpeg is a test that
  // validates today and cannot be sat tomorrow.
  const readyTracks = (tracks.data?.items ?? []).filter((t) => t.status === "ready");

  return (
    <form
      className="add-section"
      onSubmit={(event) => {
        event.preventDefault();
        setError(null);
        add.mutate();
      }}
    >
      <label htmlFor="skill">Skill</label>
      <select
        id="skill"
        value={skill}
        onChange={(event) => {
          setSkill(event.target.value as "reading" | "listening");
          setSourceXid("");
        }}
      >
        <option value="reading">Reading</option>
        <option value="listening">Listening</option>
      </select>

      <label htmlFor="section-title">Title</label>
      <input
        id="section-title"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        placeholder={skill === "reading" ? "Passage 1" : "Section 1"}
        required
      />

      <label htmlFor="source">
        {skill === "reading" ? "Passage" : "Audio track"}
      </label>
      <select
        id="source"
        value={sourceXid}
        onChange={(event) => setSourceXid(event.target.value)}
      >
        <option value="">— none yet —</option>
        {skill === "reading"
          ? (passages.data?.items ?? [])
              // A passage with no current version cannot be attached — an
              // import that never linked one leaves exactly that — and an
              // option carrying an empty value is a choice that silently
              // attaches nothing. Omitted rather than offered and broken.
              .filter((passage) => passage.current_version?.xid)
              .map((passage) => (
                <option key={passage.xid} value={passage.current_version!.xid}>
                  {passage.title}
                </option>
              ))
          : readyTracks.map((track) => (
              <option key={track.xid} value={track.xid}>
                {track.title}
                {track.duration_ms ? ` (${Math.round(track.duration_ms / 1000)}s)` : ""}
              </option>
            ))}
      </select>
      {skill === "reading" && passages.isError && (
        <p className="error">{problemText(passages.error)}</p>
      )}
      {skill === "listening" && tracks.isError && (
        <p className="error">{problemText(tracks.error)}</p>
      )}
      {skill === "listening" && tracks.data && readyTracks.length === 0 && (
        <p className="muted">
          No audio has finished processing yet. Upload one under Audio, or add the
          section now and attach it once ingest completes.
        </p>
      )}

      <label htmlFor="minutes">Time limit (minutes)</label>
      <input
        id="minutes"
        value={minutes}
        onChange={(event) => setMinutes(event.target.value)}
        inputMode="numeric"
        pattern="^[0-9]{1,3}$"
        placeholder={skill === "reading" ? "20" : "10"}
      />

      {skill === "listening" && (
        <label className="choice">
          <input
            type="checkbox"
            checked={playOnce}
            onChange={(event) => setPlayOnce(event.target.checked)}
          />
          Play once — students cannot replay this in exam mode
        </label>
      )}

      {error && <p className="error">{error}</p>}
      <div className="row">
        <button disabled={add.isPending}>{add.isPending ? "Adding…" : "Add"}</button>
        <button type="button" className="link" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}
