/**
 * The transcript for one listening track.
 *
 * "Optional transcript upload, used for post-exam review, never exposed during
 * the exam." Both halves are enforced by the server — `Action.EDIT` to read it
 * at all, and a hard refusal while the caller has a live attempt on a paper
 * using this track — so this screen does not re-implement either. What it must
 * get right is the shape of what goes in.
 *
 * **The timings are the feature.** Post-exam review cuts an excerpt by OVERLAP
 * against each question group's audio window, so a segment with the wrong span
 * attaches the wrong words to a question — and "why was my answer marked wrong"
 * is the single most useful thing this product tells a student. A wall of
 * untimed text would upload fine and produce an empty excerpt for every
 * question, silently.
 *
 * So this takes a paste of WebVTT or SRT, which is how transcripts actually
 * exist, and shows exactly what will be stored before it is stored. Nobody types
 * a hundred timed segments into a form.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { type Segment, formatMs, overlaps, parseSubtitles } from "./transcript";

export function TranscriptEditor({ trackXid, title, onClose }: {
  trackXid: string;
  title: string;
  onClose: () => void;
}) {
  const queries = useQueryClient();
  const [pasted, setPasted] = useState("");
  const [error, setError] = useState<string | null>(null);

  const existing = useQuery({
    queryKey: ["transcript", trackXid],
    queryFn: async () => {
      const { data, error: failure, response } = await api.GET(
        "/audio-tracks/{xid}/transcript", { params: { path: { xid: trackXid } } });
      // 404 is the ordinary state for a track nobody has transcribed, not an
      // error to shout about. Anything else is worth showing.
      if (response.status === 404) return null;
      if (failure) throw failure;
      return data;
    },
  });

  const parsed = pasted.trim() ? parseSubtitles(pasted) : null;
  const segments: Segment[] = parsed?.segments ?? [];
  const clashes = overlaps(segments);

  const save = useMutation({
    mutationFn: async () => {
      if (segments.length === 0) throw new Error("Nothing to save yet.");
      const { error: failure } = await api.PUT("/audio-tracks/{xid}/transcript", {
        params: { path: { xid: trackXid } },
        // Every segment, every time. The endpoint REPLACES rather than merges,
        // and a partial PUT is how a finished transcript becomes a shorter one.
        body: { language: "en", segments },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setPasted("");
      void queries.invalidateQueries({ queryKey: ["transcript", trackXid] });
      void queries.invalidateQueries({ queryKey: ["audio-tracks"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const stored = existing.data?.segments ?? [];

  return (
    <div className="issued">
      <h2>Transcript · {title}</h2>
      <p className="muted">
        Used for post-exam review only, and never shown during the exam. A student
        sees the few lines around the question they are asking about, not the
        whole script.
      </p>
      {error && <p className="error">{error}</p>}
      {existing.isError && <p className="error">{problemText(existing.error)}</p>}

      {stored.length > 0 && (
        <p>
          <strong>{stored.length} segments</strong> stored, covering{" "}
          {formatMs(stored[0]?.start_ms ?? 0)}–
          {formatMs(stored[stored.length - 1]?.end_ms ?? 0)}. Pasting below
          replaces all of them.
        </p>
      )}

      <label htmlFor="t-paste">Paste the subtitle file</label>
      <textarea
        id="t-paste"
        rows={8}
        value={pasted}
        onChange={(event) => setPasted(event.target.value)}
        placeholder={"WEBVTT\n\n00:00:00.000 --> 00:00:04.500\n"
          + "NARRATOR: You will hear a conversation between a student and a receptionist."}
      />
      <p className="muted">
        WebVTT or SRT. The timings are what make review work — each question shows
        the words spoken inside its own span of the recording, so a transcript
        without them uploads happily and explains nothing.
      </p>

      {parsed && parsed.problems.length > 0 && (
        <>
          {/* Every problem at once, each naming its line. An author fixing a
              hundred-cue file one upload at a time gives up. */}
          <ul className="report">
            {parsed.problems.map((problem, index) => (
              <li key={index} className="error">{problem}</li>
            ))}
          </ul>
          {segments.length > 0 && (
            <p className="muted">
              The {segments.length} readable cue{segments.length === 1 ? "" : "s"}{" "}
              below can still be saved — but the lines above will be missing from
              review.
            </p>
          )}
        </>
      )}

      {segments.length > 0 && (
        <>
          <h3>What will be stored</h3>
          {clashes > 0 && (
            <p className="error">
              {clashes} segment{clashes === 1 ? "" : "s"} overlap the one before.
              Review picks every segment crossing a question's window, so an
              overlap shows the same words twice.
            </p>
          )}
          <div className="scroll">
            <table>
              <thead>
                <tr><th>From</th><th>To</th><th>Speaker</th><th>Words</th></tr>
              </thead>
              <tbody>
                {segments.slice(0, 12).map((segment, index) => (
                  <tr key={index}>
                    <td className="num">{formatMs(segment.start_ms)}</td>
                    <td className="num">{formatMs(segment.end_ms)}</td>
                    <td className="muted">{segment.speaker ?? ""}</td>
                    <td>{segment.text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {segments.length > 12 && (
            <p className="muted">…and {segments.length - 12} more.</p>
          )}
        </>
      )}

      <div className="row">
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending || segments.length === 0}
        >
          {save.isPending
            ? "Saving…"
            : `Save ${segments.length} segment${segments.length === 1 ? "" : "s"}`}
        </button>
        <button type="button" className="link" onClick={onClose}>Close</button>
      </div>
    </div>
  );
}
