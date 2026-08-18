/**
 * The answer outbox: the thing that stops a student losing an exam.
 *
 * `POST /attempts/{xid}/answers` is described in its own contract as "the
 * load-bearing endpoint of the whole product". This is the client half of that
 * bargain, and the rules it has to keep are not negotiable:
 *
 *   * **Nothing is held only in React state.** A keystroke is written to
 *     IndexedDB before anything else happens. A tab crash, an OS kill, a
 *     dropped connection, a closed lid — the answers are on disk and replay on
 *     the next flush. React state is a rendering convenience; this is the record.
 *   * **Batched, not per-keystroke.** Every 5-10 s and on blur. A request per
 *     keystroke keeps the radio awake for an hour on a phone and turns one
 *     dropped packet into one lost answer.
 *   * **`client_seq` increases per slot.** The server discards a delta whose seq
 *     is below what it holds, so an out-of-order arrival on a flaky link cannot
 *     resurrect an older answer. This is what makes a blind retry safe.
 *   * **`Idempotency-Key` per flush.** A retry after a timeout replays the
 *     stored response instead of applying the batch twice.
 *   * **The response is the clock sync.** Every successful flush carries
 *     `server_now` and `expires_at`, so the countdown re-anchors continuously
 *     for free and needs no WebSocket.
 *
 * ── why IndexedDB and not localStorage ──────────────────────────────────────
 *
 * `localStorage` is synchronous and blocks the main thread, which during typing
 * is the one thread that must not stall. It also has a ~5 MB ceiling shared with
 * everything else on the origin, and it stores strings, so every write would be
 * a full `JSON.stringify` of the queue. IndexedDB is asynchronous, stores
 * structured values, and is the right tool even though its API is older and
 * uglier — which is why this module exists rather than the calls being inline.
 */

export type Delta = {
  question_version_xid: string;
  slot_key: string;
  /**
   * The value for THIS slot, not a whole response object.
   *
   * The delta already names its `slot_key`, so the server assembles
   * `{slots: {...}}` itself — which is what lets a half-finished table save the
   * cells that are done. `string[]` is for `mcq_multi`, the one type of the
   * seventeen whose answer is a list.
   */
  response: string | string[] | null;
  client_seq: number;
  time_spent_ms?: number;
};

/**
 * A queued delta plus the local row id, so a successful flush can delete it.
 *
 * `refused` is set when the SERVER declined this delta on its merits —
 * `schema_invalid`, `unknown_slot`, `stale_seq`. Such a row can never be
 * accepted by sending it again, so it leaves the retry queue; but it is not
 * deleted, because the outbox is the record and a delta the server never stored
 * is exactly the one worth still having after a reload.
 */
export type Row = Delta & { id?: number; refused?: string };

const DB_NAME = "ielts.attempts";
const STORE = "outbox";
const VERSION = 1;

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) {
        // Keyed by an autoincrementing id so insertion order is flush order,
        // and indexed by attempt so two attempts in two tabs cannot flush each
        // other's answers.
        const store = db.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
        store.createIndex("attempt", "attempt", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    // `request.error` is `DOMException | null`; a null rejection reason is
    // untraceable, so it never leaves this module bare.
    request.onerror = () => reject(request.error ?? new Error(`IndexedDB ${DB_NAME} failed to open`));
  });
}

function tx<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return open().then((db) => new Promise<T>((resolve, reject) => {
    const transaction = db.transaction(STORE, mode);
    const request = run(transaction.objectStore(STORE));
    request.onsuccess = () => resolve(request.result);
    // `request.error` is `DOMException | null`; a null rejection reason is
    // untraceable, so it never leaves this module bare.
    request.onerror = () => reject(request.error ?? new Error(`IndexedDB ${DB_NAME} failed to open`));
    transaction.oncomplete = () => db.close();
  }));
}

/**
 * Queue one delta. Called on every change, before anything else.
 *
 * Deliberately NOT deduplicated here. Two edits to the same slot are two rows
 * with two sequence numbers, and the server keeps the higher one — collapsing
 * them on the client would mean deciding which is newer with a clock that is not
 * trusted for anything else in this product.
 */
export async function queue(attempt: string, delta: Delta): Promise<void> {
  await tx("readwrite", (store) => store.add({ attempt, ...delta }));
}

async function rows(attempt: string): Promise<Row[]> {
  // `getAll()` is typed `IDBRequest<any[]>` by lib.dom — the store is
  // untyped at runtime, so the shape has to be asserted somewhere and here
  // is the one place it is written.
  const all = await tx<Row[]>("readonly", (store) => store.getAll() as IDBRequest<Row[]>);
  return all.filter((row) => (row as Row & { attempt?: string }).attempt === attempt);
}

/** Everything waiting to be SENT for this attempt, oldest first.
 *
 *  Refused rows are excluded: the server has already declined them and sending
 *  the identical bytes again gets the identical refusal, so leaving them here
 *  would be a flush loop that never drains. */
export async function pending(attempt: string): Promise<Row[]> {
  return (await rows(attempt)).filter((row) => !row.refused);
}

/** The deltas the server refused, which is what the screen has to tell the
 *  student about — they typed an answer that is nowhere but this device. */
export async function refusedRows(attempt: string): Promise<Row[]> {
  return (await rows(attempt)).filter((row) => row.refused);
}

/** Drop rows the server has accepted. */
export async function forget(ids: readonly number[]): Promise<void> {
  if (!ids.length) return;
  const db = await open();
  await new Promise<void>((resolve, reject) => {
    const transaction = db.transaction(STORE, "readwrite");
    const store = transaction.objectStore(STORE);
    for (const id of ids) store.delete(id);
    transaction.oncomplete = () => { db.close(); resolve(); };
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
  });
}

/**
 * Mark rows the server declined, rather than deleting them.
 *
 * They were deleted along with the accepted ones, so an answer the server
 * refused was gone from disk, gone from the server, and present only in React
 * state — surviving exactly until the tab was reloaded, which is the one
 * situation this whole module exists for.
 */
export async function refuse(marks: readonly { id: number; reason: string }[]): Promise<void> {
  if (!marks.length) return;
  const db = await open();
  await new Promise<void>((resolve, reject) => {
    const transaction = db.transaction(STORE, "readwrite");
    const store = transaction.objectStore(STORE);
    for (const mark of marks) {
      const read = store.get(mark.id);
      read.onsuccess = () => {
        const row = read.result as Row | undefined;
        if (row) store.put({ ...row, refused: mark.reason });
      };
    }
    transaction.oncomplete = () => { db.close(); resolve(); };
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
  });
}

/** Clear an attempt entirely, once it is submitted and scored. Refused rows go
 *  too: the paper is marked, and nothing can be done about them any more. */
export async function drop(attempt: string): Promise<void> {
  const all = await rows(attempt);
  await forget(all.map((r) => r.id!).filter((id) => id !== undefined));
}

// ── the pure parts, which is where the rules actually live ──────────────────

/**
 * The next sequence number for a slot.
 *
 * Per slot, not global: the server compares seq per `(question, slot)`, so a
 * global counter would work but would make every slot's history depend on
 * typing order in every other slot — and a resumed attempt would have to
 * reconstruct it. Per-slot state is small and local.
 */
export function nextSeq(seqs: Readonly<Record<string, number>>, key: string): number {
  return (seqs[key] ?? 0) + 1;
}

/** The key a slot's sequence is tracked under. */
export function slotKey(questionVersionXid: string, slot: string): string {
  return `${questionVersionXid}:${slot}`;
}

/**
 * Which rows to send, oldest first, capped.
 *
 * The contract permits 200 deltas per call. Sending more in one request risks a
 * body the proxy refuses; sending them one at a time on a Tashkent connection is
 * an hour of round trips.
 */
export const MAX_BATCH = 200;

export function batch(rows: readonly Row[]): Row[] {
  return [...rows].slice(0, MAX_BATCH);
}

/**
 * Keep only the newest delta per slot when replaying a large backlog.
 *
 * Used ONLY when the queue has grown past a batch — a student who typed for ten
 * minutes offline has hundreds of rows for a handful of slots, and the server
 * will discard all but the last of each anyway. Collapsing them client-side
 * turns four flushes into one. Everything dropped here is provably superseded:
 * same slot, lower seq.
 */
export function collapse(rows: readonly Row[]): Row[] {
  const newest = new Map<string, Row>();
  for (const row of rows) {
    const key = slotKey(row.question_version_xid, row.slot_key);
    const held = newest.get(key);
    if (!held || row.client_seq > held.client_seq) newest.set(key, row);
  }
  // Insertion order of a Map is first-seen, so re-sort by id to preserve the
  // original queue order rather than the order slots were first touched.
  return [...newest.values()].sort((a, b) => (a.id ?? 0) - (b.id ?? 0));
}

/** What the server said about one delta it would not store. */
export type Rejection = {
  question_version_xid: string;
  slot_key: string;
  reason?: string;
};

/**
 * Split a flushed batch into what to delete and what to mark refused.
 *
 * The whole batch was deleted, rejections included. The server names a
 * rejection by `(question, slot)` rather than by our local row id — because it
 * has never heard of our row ids — so the join is here, and more than one row
 * in a batch can carry the same slot when a student typed twice between
 * flushes. Every one of them is refused: they are the same slot at different
 * sequence numbers, and the server declined the slot.
 */
export function partition(rows: readonly Row[], rejected: readonly Rejection[]): {
  accepted: number[];
  refused: { id: number; reason: string }[];
} {
  const refusedSlots = new Map<string, string>();
  for (const r of rejected) {
    refusedSlots.set(slotKey(r.question_version_xid, r.slot_key), r.reason ?? "refused");
  }
  const accepted: number[] = [];
  const refused: { id: number; reason: string }[] = [];
  for (const row of rows) {
    if (row.id === undefined) continue;
    const reason = refusedSlots.get(slotKey(row.question_version_xid, row.slot_key));
    if (reason === undefined) accepted.push(row.id);
    else refused.push({ id: row.id, reason });
  }
  return { accepted, refused };
}

/** A flush in progress: the key it carries, and the rows it is carrying. */
export type InFlight = { key: string; ids: readonly number[] };

/**
 * The `Idempotency-Key` for a flush, reused across a RETRY of the same batch.
 *
 * It was minted inline at the call site — `attempt.flush(xid, rows,
 * attempt.idempotencyKey())` — so every attempt at the same batch carried a
 * fresh UUID and the server could not recognise the retry. The header was being
 * sent and doing nothing, which is the worst of both: the cost of the mechanism
 * and none of the protection. A timeout that actually delivered would apply the
 * batch a second time on the retry.
 *
 * Bound to the row ids, not just held: reusing a key for a batch that has since
 * gained a keystroke is `409 idempotency_key_reused`, and a client that then
 * retried with the same key again would stall for ever on its own header. Same
 * rows means same key; anything else means a new one.
 */
export function flushKey(previous: InFlight | null, ids: readonly number[],
                         mint: () => string): InFlight {
  const same = previous
    && previous.ids.length === ids.length
    && previous.ids.every((id, i) => id === ids[i]);
  return same ? previous : { key: mint(), ids: [...ids] };
}
