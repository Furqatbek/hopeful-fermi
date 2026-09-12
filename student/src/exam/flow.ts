/**
 * The runner's sequences, without React.
 *
 * `Runner.tsx` owns the refs and the state; what it used to own as well was the
 * ORDER of the load-bearing calls — outbox, then network, then delete what was
 * taken and mark what was refused; flush, then submit, then drop — and none of
 * that had a test, because it was written inline in a component that has no
 * component tests and cannot get any without a DOM. The clock, the outbox's
 * pure parts and the marking verdicts were covered; the wiring between them,
 * which is where an exam is actually lost, was not.
 *
 * So the sequences live here as plain functions over injected I/O. The runner
 * delegates and keeps the `setState` calls. Nothing in this file touches the
 * network, IndexedDB or the DOM directly, which is what lets `flow.test.ts`
 * drive every branch with a handful of fakes.
 */

import { type Clock } from "./clock";
import { type Flushed } from "./attempt";
import {
  MAX_BATCH, batch, collapse, flushKey, partition,
  type Delta, type InFlight, type Row,
} from "./outbox";

// ── the flush loop ──────────────────────────────────────────────────────────

/** The I/O one flush needs. Only the parts that touch a store or a socket are
 *  injected; the pure parts come straight from `outbox.ts`. */
export type FlushDeps = {
  pending: (xid: string) => Promise<Row[]>;
  flush: (xid: string, rows: readonly Delta[], key: string) => Promise<Flushed>;
  forget: (ids: readonly number[]) => Promise<void>;
  refuse: (marks: readonly { id: number; reason: string }[]) => Promise<void>;
  mint: () => string;
};

export type FlushOutcome = {
  /** What the next flush must carry: the key of an undelivered batch, or null. */
  inFlight: InFlight | null;
  /** The clock sync, present only when the server answered. */
  clock?: Clock;
  /** What the server declined, with its reason for each. */
  refused: { id: number; reason: string }[];
  /** True only when the round trip succeeded AND nothing is left waiting. */
  drained: boolean;
};

/**
 * One pass of the flush loop.
 *
 * A storage failure (`pending`, `forget`, `refuse` rejecting) propagates: the
 * caller decides what silence means. A NETWORK failure is caught here and
 * reported through `inFlight`, because what must survive it is the key.
 */
export async function flushOnce(
  deps: FlushDeps, xid: string, inFlight: InFlight | null,
): Promise<FlushOutcome> {
  const waiting = await deps.pending(xid);
  if (!waiting.length) return { inFlight, refused: [], drained: true };
  // Collapse only when there is a genuine backlog: everything dropped is
  // provably superseded (same slot, lower seq), and below a batch there is
  // nothing to gain.
  const rows = batch(waiting.length > MAX_BATCH ? collapse(waiting) : waiting);
  // One key per BATCH, held until the batch is delivered. Minting it at the
  // call site gave every retry a fresh UUID, so the header was sent and the
  // server could not recognise a retry as one.
  const ids = rows.map((r) => r.id!).filter((id) => id !== undefined);
  const carrying = flushKey(inFlight, ids, deps.mint);
  let sent: Flushed;
  try {
    sent = await deps.flush(xid, rows, carrying.key);
  } catch {
    // A failed flush is not an error the student can act on. The deltas stay
    // in IndexedDB and go again on the next tick — which is the entire point
    // of the outbox, and showing a banner here would make a two-second wifi
    // dropout look like data loss. The key goes again with them.
    return { inFlight: carrying, refused: [], drained: false };
  }

  // Delete what the server took; MARK what it refused. Deleting a refusal
  // too meant the student's answer was gone from disk, gone from the
  // server, and present only in React state — which survives exactly until
  // the reload this module exists to survive.
  const { accepted, refused } = partition(rows, sent.rejected);
  await deps.forget(accepted);
  await deps.refuse(refused);
  // Read back rather than inferred: a keystroke queued during the round trip
  // is waiting too, and `pending` excludes what was just marked refused.
  const drained = (await deps.pending(xid)).length === 0;
  return { inFlight: null, clock: sent.clock, refused, drained };
}

// ── submit ──────────────────────────────────────────────────────────────────

export type SubmitDeps = {
  /** The runner's flush: resolves true when the queue drained. Never rejects. */
  flush: () => Promise<boolean>;
  pending: (xid: string) => Promise<Row[]>;
  submit: (xid: string, key: string, finalAnswers?: readonly Delta[]) => Promise<unknown>;
  drop: (xid: string) => Promise<void>;
  mint: () => string;
};

/**
 * The submit's idempotency key and its latch, held by the runner in a ref.
 *
 * `key` is bound to the row ids it was sent with, exactly as `outbox.flushKey`
 * binds a flush key: the body can now vary between retries, and reusing a key
 * for a different body is `409 idempotency_key_reused`. A fresh key when the
 * remainder changed is safe — the server returns the current run for an
 * attempt that is already submitted, so a retry can never score twice.
 */
export type SubmitState = { key: InFlight | null; busy: boolean };

/**
 * Flush, then submit, then drop — in that order, and each only if the one
 * before it held up.
 *
 * Everything on disk goes first, or the last thing typed is never marked. The
 * flush fails silently by design, and the submit used to go ahead regardless:
 * it froze the attempt server-side and then `drop()` deleted the rows the flush
 * had not delivered — so the last few seconds of typing were gone from the
 * server, gone from disk, and present nowhere. Now the flush is retried once
 * (both inside the 30 s grace), and whatever is STILL waiting rides in the
 * submit body as `final_answers`, which the contract has carried for exactly
 * this since it was written. Rows are only dropped after the submit resolved,
 * so a submit that fails leaves them on disk for the retry.
 *
 * Resolves `"busy"` and does nothing when a submit is already in flight: the
 * timer-zero effect and the Finish button both call this, and React state
 * lags a synchronous double-call by one render.
 */
export async function submitFlow(
  deps: SubmitDeps, xid: string, state: SubmitState,
): Promise<"submitted" | "busy"> {
  if (state.busy) return "busy";
  state.busy = true;
  try {
    let drained = await deps.flush();
    if (!drained) drained = await deps.flush();
    let remainder: Row[] = [];
    if (!drained) {
      const left = await deps.pending(xid);
      // The same cap as a flush. Past 200 distinct slots something is already
      // badly wrong, and the server refuses a larger body outright.
      remainder = batch(left.length > MAX_BATCH ? collapse(left) : left);
    }
    const ids = remainder.map((r) => r.id!).filter((id) => id !== undefined);
    // Held BEFORE the call, so a submit that times out after landing retries
    // with the key the server already knows.
    state.key = flushKey(state.key, ids, deps.mint);
    await deps.submit(xid, state.key.key, remainder.length ? remainder : undefined);
    // The submit landed and the paper is marked; a store that cannot be
    // cleared afterwards is not a reason to show a failure, and re-submitting
    // to retry the clear would only return the same run.
    await deps.drop(xid).catch(() => undefined);
    // Stays latched: the paper is gone, and a late second call from the
    // timer effect must not race the navigation away.
    return "submitted";
  } catch (failure) {
    state.busy = false;
    throw failure;
  }
}

// ── the timer-zero retry ─────────────────────────────────────────────────────

/** Where the back-off stops growing. Two tries a minute is harmless. */
export const MAX_RETRY_MS = 30_000;

/**
 * How long to wait before automatic submit attempt number `tries` (zero-based),
 * or null to stop.
 *
 * The timer-zero effect used to re-arm synchronously: each failure flipped
 * `submitting` back, the effect re-ran, `remaining()` was still zero, and the
 * next submit went out as fast as the last one failed. A room of students whose
 * clocks hit zero together during a backend hiccup was a few hundred submits a
 * second against the rate limiter that is set on the promise no plausible
 * client reaches it. Backing off costs the student nothing: the server accepts a
 * late submit inside its grace window, and the sweeper submits an expired
 * attempt on its own.
 *
 * The first try goes at once. `attempt_voided` is permanent — nothing a retry
 * can change — so it stops the ladder; everything else keeps climbing to the cap.
 */
export function nextDelay(tries: number, failure?: string): number | null {
  if (failure === "attempt_voided") return null;
  if (tries <= 0) return 0;
  return Math.min(MAX_RETRY_MS, 2_000 * 2 ** (tries - 1));
}

// ── the section clock ────────────────────────────────────────────────────────

/**
 * Should the runner move on when the DISPLAYED section clock reaches zero?
 *
 * Only when the section has a deadline strictly earlier than the attempt's
 * (otherwise the attempt clock owns the moment and submits the paper), only
 * from the furthest section entered (navigating back into an already-closed
 * section shows 0:00 and a notice; it must not bounce the student forward
 * again), and never from the last section, where there is nowhere to go and
 * the paper-level auto-submit does the right thing.
 */
export function advancesOnSectionExpiry(args: {
  sectionExpiresAt: string | null | undefined;
  attemptExpiresAt: string;
  position: number;
  highestEntered: number;
  isLast: boolean;
}): boolean {
  const { sectionExpiresAt, attemptExpiresAt, position, highestEntered, isLast } = args;
  if (sectionExpiresAt === null || sectionExpiresAt === undefined) return false;
  if (!(Date.parse(sectionExpiresAt) < Date.parse(attemptExpiresAt))) return false;
  return position === highestEntered && !isLast;
}

// ── keyboard ─────────────────────────────────────────────────────────────────

/** The parts of an event target the arrow-key guard reads. Structural rather
 *  than `instanceof`, so the rule can be tested without a DOM. */
export type KeyTarget = {
  tagName?: string;
  isContentEditable?: boolean;
  getAttribute?: (name: string) => string | null;
} | null;

/**
 * Is something focused already spending this arrow key?
 *
 * Arrow keys step the palette (0013 §5) only when nothing focused is using
 * them. The reading divider (also §5) resizes the panes on Left/Right and
 * `preventDefault`s, and a closed `<select>` on Windows/Linux changes its value
 * on the same keys — with the old input/textarea-only guard, one key resized
 * AND left the question, or picked a heading AND left the question. That is
 * the Finish-button class of bug from `Chrome.tsx`: the key that should do X
 * did Y as well.
 */
export function claimsArrowKey(target: KeyTarget, defaultPrevented: boolean): boolean {
  if (defaultPrevented) return true;
  if (!target) return false;
  const tag = (target.tagName ?? "").toUpperCase();
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (target.isContentEditable) return true;
  return target.getAttribute?.("role") === "separator";
}
