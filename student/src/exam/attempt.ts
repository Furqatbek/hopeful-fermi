/**
 * The attempt lifecycle, against the real API.
 *
 * The order matters and every step depends on the one before it, which is why
 * this is one module rather than calls scattered through components:
 *
 *   POST /attempts                              start (idempotent)
 *   GET  /attempts/{xid}/payload                the frozen paper
 *   POST /attempts/{xid}/sections/{n}/enter     starts THAT section's clock
 *   POST /attempts/{xid}/sections/{n}/audio-grant   the single play (exam mode)
 *   POST /attempts/{xid}/answers                autosave, and the clock sync
 *   POST /attempts/{xid}/submit                 idempotent
 *   GET  /attempts/{xid}/result                 the band
 *
 * Two of them are irreversible and both carry an `Idempotency-Key`: starting
 * burns one of `max_attempts`, and submitting ends the sitting. A retry after a
 * dropped response must return the same attempt and the same score run, not a
 * second of either.
 */

import { api } from "../api/client";
import { sync, type Clock } from "./clock";
import { type Delta, type Rejection } from "./outbox";

/** A fresh key per irreversible operation. */
export function idempotencyKey(): string {
  return crypto.randomUUID();
}

export type Started = {
  xid: string;
  status: string;
  mode: "exam" | "practice";
  attempt_no: number;
  expires_at: string;
  server_now: string;
  seconds_remaining: number;
};

export async function start(
  assignmentXid: string, mode: "exam" | "practice", key: string,
): Promise<Started> {
  const { data, error } = await api.POST("/attempts", {
    // Burns an attempt. The key is created ONCE by the caller and reused across
    // retries — generating it here would mint a new one per attempt to send,
    // which is the same as having no idempotency at all.
    params: { header: { "Idempotency-Key": key } },
    // `mode` is required and must match the assignment's. The worked example in
    // docs/api/student-app.md shows only `assignment_xid`, which is why this was
    // wrong until the generated client refused it — the contract is the
    // authority, and this is the codegen earning its place.
    body: { assignment_xid: assignmentXid, mode },
  });
  if (error) throw error;
  return data as Started;
}

/**
 * The assignment this attempt is for.
 *
 * `POST /attempts` needs its `mode`, and there is no `GET /assignments/{xid}` —
 * the listing is the only way to read one, so this finds it there. Cheap, and it
 * is usually already warm from the home screen.
 */
export async function assignment(assignmentXid: string) {
  const { data, error } = await api.GET("/assignments", { params: { query: {} } });
  if (error) throw error;
  return (data.items ?? []).find((a) => a.xid === assignmentXid);
}

/** The snapshot the student sits. Frozen at publish and free of answer keys. */
export async function payload(attemptXid: string) {
  const { data, error } = await api.GET("/attempts/{xid}/payload", {
    params: { path: { xid: attemptXid } },
  });
  if (error) throw error;
  return data;
}

/** Current server time and deadline, for the countdown and on resume. */
export async function state(attemptXid: string) {
  const { data, error } = await api.GET("/attempts/{xid}", {
    params: { path: { xid: attemptXid } },
  });
  if (error) throw error;
  return data;
}

/**
 * Enter a section. This is what starts that section's clock server-side, so it
 * happens when the student arrives at the section and not when the paper loads.
 */
export async function enter(attemptXid: string, position: number) {
  const { data, error } = await api.POST("/attempts/{xid}/sections/{position}/enter", {
    params: { path: { xid: attemptXid, position } },
  });
  if (error) throw error;
  return data;
}

/**
 * Mint the audio grant. **In exam mode this succeeds exactly once** — a second
 * call answers `409 audio_already_played`.
 *
 * So it is called when the student has COMMITTED to playing, never on load.
 * There is no way back, and a component that mints on mount would spend the
 * single play on a student who opened the section to read ahead.
 */
export async function audioGrant(attemptXid: string, position: number) {
  const { data, error } = await api.POST("/attempts/{xid}/sections/{position}/audio-grant", {
    params: { path: { xid: attemptXid, position } },
  });
  if (error) throw error;
  return data;
}

export type Flushed = {
  accepted: number;
  rejected: readonly Rejection[];
  last_accepted_seq: number;
  clock: Clock;
};

/**
 * Send a batch of deltas. The response IS the clock sync — which is why exam
 * timing needs no WebSocket, and why the caller should fold the returned clock
 * back into the countdown on every flush.
 *
 * `data` is used directly rather than cast, which is new: the contract used to
 * under-declare this response, missing `question_version_xid` and marking
 * `slot_key`/`reason` optional though the server always sends both — so the
 * generated type couldn't satisfy `outbox.partition()`'s `Rejection[]`, and the
 * cast to `unknown[]` was the escape hatch. The server was always right; the
 * spec just never said so. Fixed in `openapi.yaml` instead of here.
 */
export async function flush(
  attemptXid: string, deltas: readonly Delta[], key: string,
): Promise<Flushed> {
  const { data, error } = await api.POST("/attempts/{xid}/answers", {
    params: { path: { xid: attemptXid }, header: { "Idempotency-Key": key } },
    body: { deltas: [...deltas] },
  });
  if (error) throw error;
  return {
    accepted: data.accepted,
    rejected: data.rejected,
    last_accepted_seq: data.last_accepted_seq,
    clock: sync({ serverNow: data.server_now, expiresAt: data.expires_at }),
  };
}

/**
 * Submit. Idempotent, and there is a **30-second grace window after
 * `expires_at`** — a submit that lands late is accepted and the overrun
 * recorded, so the client must NOT refuse at +1s. If it fails entirely, retry
 * with the same key: answers already saved are safe, and the sweeper submits an
 * abandoned attempt on its own.
 *
 * `finalAnswers` is the contract's `final_answers` — "last outbox flush, applied
 * before freezing". It carries whatever the flush just before submit could not
 * deliver: that flush fails silently by design, and a submit that went ahead
 * anyway froze the attempt and then dropped the undelivered rows from disk. The
 * body is omitted when there is nothing left, so a body-less submit is
 * byte-for-byte what it always was.
 */
export async function submit(attemptXid: string, key: string, finalAnswers?: readonly Delta[]) {
  const { data, error } = await api.POST("/attempts/{xid}/submit", {
    params: { path: { xid: attemptXid }, header: { "Idempotency-Key": key } },
    ...(finalAnswers?.length ? { body: { final_answers: [...finalAnswers] } } : {}),
  });
  if (error) throw error;
  return data;
}

export async function result(attemptXid: string) {
  const { data, error } = await api.GET("/attempts/{xid}/result", {
    params: { path: { xid: attemptXid } },
  });
  if (error) throw error;
  return data;
}
