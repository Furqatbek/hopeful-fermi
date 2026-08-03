/**
 * The resumable audio upload, as a plain function so it can be tested without a
 * DOM. The screen renders what this reports; it decides nothing.
 *
 * Four steps, and the shape is the server's rather than a convention:
 *
 *   1. `POST /audio-tracks` — creates the track AND opens an upload session,
 *      returning presigned part URLs with an `offset`/`length` per part.
 *   2. `PUT` each part straight to storage. **The bytes never pass through the
 *      application process** — that is the point of presigning, and on a 4 vCPU
 *      box shared with live exams it is the difference between one teacher's
 *      40 MB upload being free and it starving forty students mid-mock.
 *   3. `POST /uploads/{xid}` with `{n, etag}` per part. Answers 202: the object
 *      is assembled but not yet playable.
 *   4. Poll the track until `status` leaves `processing`. Ingest measures
 *      loudness, normalises and encodes a delivery file; a listening section
 *      cannot be composed until that lands.
 *
 * `offset`/`length` come from the server, so a resumed session slices exactly
 * the parts it still needs. `parts_received` lists what it already holds — on a
 * metered Uzbek mobile connection, re-sending a stored part is somebody's money.
 */

import { api } from "../../api/client";
import type { components } from "../../api/schema";

/** Exactly what `content.media.ALLOWED_AUDIO` accepts, via the contract — so
 *  this list cannot drift from the server's without CI noticing. */
type AudioContentType =
  NonNullable<components["schemas"]["AudioTrackCreate"]["content_type"]>;

const ACCEPTED: readonly AudioContentType[] = [
  "audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp3",
  "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/aac", "audio/ogg",
  "audio/opus", "audio/flac", "audio/x-flac", "audio/webm",
];

export const ACCEPT_ATTRIBUTE = ACCEPTED.join(",");

/** A browser leaves `type` empty for an extension it does not know, and sends
 *  `audio/mp3` where the file is really MPEG. Refusing here is a courtesy: the
 *  server checks the same set, and this only saves the teacher a round trip and
 *  gives them a message that names what to convert to. */
function contentTypeOf(file: File): AudioContentType {
  const found = ACCEPTED.find((type) => type === file.type);
  if (!found) {
    throw new Error(
      file.type
        ? `${file.type} is not a supported audio format. Use WAV, MP3, M4A, AAC, OGG or FLAC.`
        : "That file's format could not be identified. Use WAV, MP3, M4A, AAC, OGG or FLAC.",
    );
  }
  return found;
}

export type Progress =
  | { phase: "creating" }
  | { phase: "hashing" }
  | { phase: "uploading"; sent: number; total: number }
  | { phase: "assembling" }
  | { phase: "transcoding" }
  | { phase: "ready"; durationMs: number | null; loudnessLufs: number | null }
  | { phase: "failed"; reason: string };

export type Attestation = {
  claim: "original" | "licensed" | "public_domain" | "permitted_excerpt";
  statement_version: string;
  licence_note?: string;
};

/**
 * sha256 of the file, hex, computed in the browser.
 *
 * Optional in the contract and worth sending: the ingest worker compares it
 * against the bytes that actually arrived and fails the track with an
 * explanation rather than publishing audio nobody can hear. A truncated upload
 * over a flaky connection is the case — it produces a valid-looking m4a of the
 * wrong length, and without this the first person to notice is a student in a
 * timed exam.
 *
 * `crypto.subtle` needs a secure context: HTTPS, or localhost in development.
 * When it is unavailable we send no checksum rather than failing the upload —
 * the server treats it as optional, and refusing to upload at all would be a
 * worse trade than losing one guard.
 */
export async function sha256Hex(file: File): Promise<string | undefined> {
  if (!globalThis.crypto?.subtle) return undefined;
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export async function uploadAudioTrack(
  file: File,
  fields: { title: string; accent?: string; attestation: Attestation },
  report: (progress: Progress) => void,
  options: { pollMs?: number; signal?: AbortSignal } = {},
): Promise<{ trackXid: string }> {
  const pollMs = options.pollMs ?? 1500;

  report({ phase: "hashing" });
  const checksum = await sha256Hex(file);

  report({ phase: "creating" });
  const { data: created, error: createFailed } = await api.POST("/audio-tracks", {
    body: {
      title: fields.title,
      ...(fields.accent ? { accent: fields.accent } : {}),
      filename: file.name,
      bytes: file.size,
      content_type: contentTypeOf(file),
      ...(checksum ? { checksum_sha256: checksum } : {}),
      attestation: fields.attestation,
    },
  });
  if (createFailed || !created) throw createFailed;

  const session = created.upload;
  const outstanding = session.presigned_urls ?? [];
  const total = outstanding.reduce((sum, part) => sum + (part.length ?? 0), 0);
  let sent = 0;

  const parts: { n: number; etag: string }[] = [];
  for (const part of outstanding) {
    if (options.signal?.aborted) throw new Error("Upload cancelled.");
    const slice = file.slice(part.offset ?? 0, (part.offset ?? 0) + (part.length ?? 0));
    const response = await fetch(part.url!, {
      method: "PUT",
      body: slice,
      ...(options.signal ? { signal: options.signal } : {}),
    });
    if (!response.ok) {
      throw new Error(`Part ${part.n} failed to upload (HTTP ${response.status}).`);
    }
    // The completion step compares these, and an S3 backend requires them. A
    // missing header is a backend that is not behaving like the one this flow
    // was written against, so say so rather than sending an empty string and
    // failing three steps later with a checksum mismatch.
    const etag = response.headers.get("ETag");
    if (!etag) throw new Error(`Part ${part.n} returned no ETag.`);
    parts.push({ n: part.n!, etag: etag.replaceAll('"', "") });
    sent += part.length ?? 0;
    report({ phase: "uploading", sent, total });
  }

  report({ phase: "assembling" });
  const { error: completeFailed } = await api.POST("/uploads/{xid}", {
    params: { path: { xid: session.xid } },
    body: { parts },
  });
  if (completeFailed) throw completeFailed;

  report({ phase: "transcoding" });
  const trackXid = created.audio_track.xid!;
  for (;;) {
    if (options.signal?.aborted) throw new Error("Cancelled.");
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    const { data: track } = await api.GET("/audio-tracks/{xid}", {
      params: { path: { xid: trackXid } },
    });
    if (!track) continue;                    // a transient read; keep waiting
    if (track.status === "ready") {
      report({
        phase: "ready",
        durationMs: track.duration_ms ?? null,
        loudnessLufs: track.loudness_lufs ?? null,
      });
      return { trackXid };
    }
    if (track.status === "failed") {
      // The worker records WHY on the asset — a loudness that is wildly out, a
      // checksum mismatch, a file ffmpeg cannot open. Showing "failed" alone
      // sends the teacher to support with nothing.
      report({ phase: "failed", reason: "Ingest failed. Open the track for details." });
      throw new Error("Ingest failed.");
    }
  }
}
