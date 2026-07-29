# Deliverable 3 — API Contract

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Depends on:** ADR-0001, `docs/design/0002-data-model.md`
- **Canonical spec:** `openapi/openapi.yaml` — single tagged document, OpenAPI 3.1.0.
  This file is the map, the rationale, and the realtime protocol.

---

## 0. Summary

| | |
|---|---|
| Paths / operations | **113 / 149** |
| Tags | 18, mapping 1:1 onto the internal modules |
| Schemas | **144**, of which 16 are WebSocket event payloads (`Rt*`) |
| Validation | `openapi-spec-validator` → **PASS** against OpenAPI 3.1.0 |
| Dangling `$ref`s | none · Undeclared tags: none · Undeclared path params: none |
| Operations missing tags/summary/responses | none |

One correctness fix worth recording: I had written 105 `nullable: true` properties,
which is **OpenAPI 3.0 syntax**. In 3.1 schemas are JSON Schema 2020-12, where
`nullable` is not a keyword — it is silently ignored, and every client generator
would have emitted non-nullable types for fields that are routinely null
(`band`, `expires_at`, `discrimination`). All 105 are now `type: [x, 'null']`.
The spec validated *before* the fix too, which is the point: this class of error
does not announce itself.

---

## 1. Conventions, and why each one

| Convention | Choice | Reason |
|---|---|---|
| Identifiers | Opaque `xid` (UUIDv7) in every path | `/tests/1234` is a scraping API. This is load-bearing for content protection, not cosmetic |
| Errors | RFC 9457 `application/problem+json` | Boring, standard, has a `code` field for client branching |
| Validation errors | **Every** finding at once, with a `path` pointer and a `fix_hint` | An author fixes one round rather than nineteen |
| Pagination | Cursor, not offset | The library sorts by `updated_at`; offsets skip and duplicate rows under concurrent edits |
| Idempotency | `Idempotency-Key` **required** on autosave, order creation, import commit | These are exactly the calls a client on a dropping connection will retry |
| Concurrency | `If-Match` / ETag on draft edits | Two teachers in one test is a real scenario; last-write-wins loses work silently |
| Time | RFC 3339 UTC + `server_now` on anything time-critical | A cheap Android clock can be minutes out. The client renders from the delta |
| Versioning | `/api/v1` in the path | Boring. Header-based versioning is cleverer and harder to debug in a proxy log |
| Permissions | A `permissions` object on detail responses | The client renders correct affordances without re-implementing the matrix or probing |

Two conventions I want to draw attention to because they are unusual:

**`permissions` on detail responses.** `GET /tests/{xid}` returns
`{edit, publish, archive, delete, share, export, regrade, clone}` as booleans,
resolved once by the central policy engine. Without this, the frontend either
re-implements the §8 matrix from Deliverable 2 (and drifts from it) or fires
speculative requests to discover what it may do. Both are worse.

**`Retry-After` on the competition key endpoint.** `POST /competitions/{xid}/key`
returns **425 Too Early** with the remaining seconds when a client asks before
T-0. That turns a synchronized-start race into a self-correcting backoff rather
than a client-side clock guess.

---

## 2. The four endpoints that carry the product

Everything else is ordinary CRUD. These four are where the design lives.

### 2.1 `POST /attempts/{xid}/answers` — autosave

The load-bearing endpoint. Five properties, each one preventing a specific way a
student loses an exam on an Uzbek mobile network:

1. **Batched.** The client flushes its IndexedDB outbox every 5–10 s or on blur.
   The radio sleeps between sends, and a dropped request costs one round trip
   rather than one answer.
2. **Idempotent.** `Idempotency-Key` is required. A retry after a timeout replays
   the stored response instead of double-applying.
3. **Ordered per slot.** Each delta carries `client_seq`. A delta whose seq is
   below the stored revision is ignored, so out-of-order delivery on a flaky link
   cannot resurrect an older answer. This is the subtle one — retries plus
   reordering without a sequence number silently corrupt answers.
4. **Partially acceptable.** A delta failing `response_schema` is rejected
   individually into `rejected[]`; the rest commit. One malformed answer must
   never cost a batch of forty.
5. **The response is the clock sync.** `server_now`, `expires_at` and
   `seconds_remaining` come back on every save. This is why exam timing needs no
   WebSocket at all — the sync rides along with traffic the client is already
   sending.

`410 Gone` carries the result when the sweeper already auto-submitted, so a
student whose connection died past the deadline lands on their score screen
rather than a dead timer.

### 2.2 `POST /test-versions/{xid}/publish` — the gate

Runs all 19 checks from Deliverable 2 §6 and returns **422 with every finding**,
each carrying a machine `code`, a `path` into the composition for deep-linking,
and a `fix_hint`. Success freezes the version, materializes the student-facing
snapshot, and writes an audit record.

Teachers get **403** unless the centre has enabled `settings.teacher_can_publish`.
That default is off, and I would argue for keeping it off: a centre's reputation
rides on its published material.

### 2.3 `POST /question-versions/{xid}/keys` — the key fix

The one that keeps school clients. It works on **published** content, which is
the entire point, and it does not violate immutability: the question version
stays frozen and a new `answer_key_versions` row supersedes the old one.

The response is the interesting part. When the item has been sat, it returns a
`regrade_preview` and a `regrade_job_xid` in dry-run state:

```json
{
  "key_version": { "version_no": 2, "reason": "key_fix" },
  "regrade_job_xid": "018f…",
  "regrade_preview": {
    "attempts_total": 142, "scores_changed": 138, "bands_changed": 41,
    "students_to_notify": 41,
    "competition_impact": [
      { "title": "March Mock Cup", "rank_changes": 12,
        "podium_changes": 1, "decision_required": true }
    ]
  }
}
```

**Nothing is regraded until `POST /regrades/{xid}/apply`.** And if
`competition_impact` is non-empty, apply returns **409** until a platform admin
has decided at `/competitions/{xid}/regrade-decisions/{job_xid}`. A leaderboard
that changes by itself looks like fraud, so the API makes that impossible without
a named human decision.

### 2.4 The two-step competition start

`GET /competitions/{xid}/lobby` (T-120s → T-0) serves the snapshot **AES-GCM
encrypted** from a Redis-cached blob, with a per-client `fetch_after` jitter
spreading 200 clients across the window. `POST /competitions/{xid}/key` at T-0
returns ~100 bytes.

The encryption is load-bearing for fairness, not theatre: without it, the payload
sitting on the device from T-120s is a two-minute reading head start for anyone
who opens devtools. Server-side timing starts from `starts_at`, never from when
the client asked.

---

## 3. Authoring surface

The largest tag group, as it should be — this is the product's moat. 54 of the
113 paths are authoring, media, import/export or governance.

Three shapes worth calling out:

**Reuse is a reference, not a copy.** `POST /test-versions/{xid}/sections` takes
`passage_version_xid`; `POST /question-group-versions/{xid}/items` takes
`question_version_xid`. Pulling an existing passage into a new test is one call
and duplicates nothing. `GET /passage-versions/{xid}/usage` shows where a shared
asset is referenced *before* an author edits it, so "I changed one passage and
broke four published mocks" cannot happen by surprise.

**Server owns derived structure.** Paragraph letters (A, B, C…) are assigned
server-side on every passage save and returned read-only in `paragraph_labels`.
`slot_keys` are extracted from the question payload rather than declared by the
client. Both exist because matching-headings references and key/blank consistency
are validated against these values — letting the client assert them would make
the publish gate check the client's opinion of itself.

**Import is dry-run-first, and adapters are replaceable.** `POST /imports`
returns 202; every adapter (DOCX, CSV, JSON) produces the same canonical Import
JSON, which is what gets validated, diffed and committed. `POST /imports/{xid}/commit`
applies the **stored** canonical JSON the author reviewed, not a re-parse that
might differ. For anyone without publish rights it lands as a draft, so bulk
import can never become a publish bypass.

`GET /imports/template` exists to set the expectation with centres: the supported
path is a locked DOCX with named styles. Parsing an arbitrary Word file a teacher
already has is an open-ended problem; parsing this template is a bounded one.

---

## 4. Realtime event contract

### 4.1 Why this is not AsyncAPI

I considered AsyncAPI 3.0 and am not recommending it. It would mean a second spec
format, a second generator and a second thing to keep in sync, for a surface of
one endpoint and ~16 event types. Instead the event payloads live in the OpenAPI
document's `components/schemas` under an `Rt` prefix, so a single
`openapi-typescript` run types both transports, and the protocol semantics —
which are where the actual complexity is — live here in prose.

Revisit if the event surface passes roughly 40 types or a second consumer appears.

### 4.2 Connection

```
POST /api/v1/realtime/ticket   →  { ticket, url, expires_at }
WS   wss://api.example.uz/realtime?ticket=<ticket>
```

Browsers cannot set headers on a WebSocket handshake, so the access token must
not go in the query string where it lands in every proxy log. The ticket is
**single-use and ~30 s**, bound to the user, and burned by the gateway at
handshake.

### 4.3 Envelope

Every frame, both directions:

```json
{ "id": "c-17", "type": "leaderboard.delta", "channel": "competition:018f…",
  "seq": 412, "ts": "2026-07-29T14:03:11Z", "data": { … } }
```

### 4.4 Channels

| Channel | Who may subscribe | Events |
|---|---|---|
| `user:{me}` | self only (implicit) | `notification`, `session.revoked` |
| `queue:{entry_xid}` | the owner | `queue.position`, `queue.matched`, `queue.expired` |
| `slot:{slot_xid}` | booked users | `slot.opened`, `slot.matching`, `slot.matched`, `slot.cancelled` |
| `pair:{pair_xid}` | the two peers | `pair.peer_joined`, `pair.peer_left`, `pair.ended`, `signal.*` |
| `competition:{xid}` | registered entrants | `competition.state`, `competition.key_released`, `leaderboard.snapshot`, `leaderboard.delta`, `competition.finalized` |
| `assignment:{xid}` | the assigning teacher | `assignment.progress` |
| `attempt:{xid}` | the attempt owner | `attempt.clock`, `attempt.force_submit` |

Channel authorization runs through the same `authz.Policy.check()` as HTTP. There
is no second permission model for the socket — that is how the two drift apart.

### 4.5 Resume, which is the part that matters here

Every channel carries a monotonic `seq`. The client stores the last seq it saw
and resumes with it:

```json
{ "type": "subscribe", "channels": ["competition:018f…"], "since_seq": 412 }
```

The server keeps a bounded Redis replay buffer (last ~100 events, ~5 min) per
channel and replays from `since_seq`. If the requested point has fallen out of
the buffer, the server sends `RtResync` naming the HTTP endpoint to reload from,
rather than silently continuing on a partial view:

```json
{ "type": "resync", "channel": "competition:018f…",
  "data": { "refetch": "/api/v1/competitions/018f…/leaderboard" } }
```

On these networks a tunnel drop mid-competition is routine, not exceptional. A
protocol without explicit resync means a student watching a leaderboard that
quietly stopped updating.

### 4.6 Throttling

| Event | Cadence |
|---|---|
| `leaderboard.delta` | ≤ 1 frame / 3 s per competition; only changed rows, plus the subscriber's own row |
| `assignment.progress` | ≤ 1 frame / 5 s |
| `attempt.clock` | 1 frame / 30 s |
| heartbeat | server ping every 25 s; client closes after 2 missed |

`leaderboard.delta` always includes `my_rank` even when the subscriber is off the
visible page — otherwise the one number they actually care about goes stale while
the board around them updates.

### 4.7 What is deliberately NOT on the socket

**Exam answers.** Restating ADR-0001 §8.2 now that both contracts exist: you told
me connections drop constantly and losing answers is unacceptable, and those two
facts argue *against* a stateful long-lived connection. A dropped WS means
reconnect, re-auth, resubscribe, and then resolve "what did the server actually
receive?" — hand-rolling reliable delivery over a transport that pretends to be
reliable. A dropped `POST` means the client retries with its idempotency key and
the server deduplicates. That is the whole failure story.

`attempt.clock` exists only so an idle tab with no pending writes still notices
its deadline. It is belt and braces; the autosave response is the primary sync.

**Media.** Signaling is relayed opaquely — the server never parses SDP or ICE. It
is a mailbox, not a media component, and audio never transits application servers.

---

## 5. Deliberate omissions

| Not in the API | Why |
|---|---|
| Bulk question-payload read | The exam endpoint inherently bulk-reads a test, so a "bulk" endpoint is cosmetic. Protection is authz + `filter` + rate limits + exposure logging + opaque ids (ADR-0001 §8.9) |
| `PUT` anywhere except transcript | Full-resource replace on versioned content invites accidental field clearing. `PATCH` with `If-Match` |
| Hard delete of published content | Nobody, including platform admin. Attempts reference it and takedowns need the evidence |
| `date_of_birth` in any response | Collected for the 18 boundary only. Absent from `User`, from leaderboards, from B2B exports |
| Password endpoints | There are no passwords. Telegram or OTP |
| GraphQL / batch endpoint | Not at 50 req/s peak |
| Webhooks out to centres | No demand yet. `content_grants` and the outbox are the seam if one appears |

---

## 6. Things to push back on

1. **`permissions` objects add response weight.** ~200 bytes on detail responses.
   I think correct client affordances are worth it; if you disagree, the fallback
   is a single `GET /me/permissions?subject=…` probe endpoint, which is worse.

2. **Two-step competition start is more machinery than 200 users strictly need.**
   The simpler fallback — serve from a Redis-cached blob at T-0 with 30 s of
   jitter — works fine at 200 and breaks around 2,000. The encryption is ~40 lines
   and buys fairness. I would build it, but it is the most skippable thing here.

3. **Cursor pagination costs you `page=3` in the UI.** Correct trade for a library
   sorted by `updated_at`, but it does mean no page-number jumping.

4. **`POST /takedowns` is unauthenticated.** A rights holder must not need an
   account to file, and I would rather absorb some spam than be unreachable to
   Cambridge's lawyers. It is rate-limited by IP and requires `sworn_statement`.

5. **`GET /media/{xid}/content` is served by the app, not a CDN.** Per-user tokens
   mean zero cache hits by construction. Right at ~20 GB/month; revisit above
   ~500 GB/month, at which point the answer is signed CDN URLs with a coarser
   grant and accepting weaker per-user attribution.

6. **The Payme endpoint is a single JSON-RPC POST**, which looks wrong next to 112
   REST paths. It is right: Payme drives a transaction state machine and calls us.
   Modelling it as REST resources would fight the protocol and break on their
   error-code contract.

---

## Deliverable 3 ends here.

Nothing blocks Deliverable 4 (implementation, tests first for the scoring engine,
publish gate, regrade path and entitlements).

One question before I start: the build order in ADR-0001 §12 puts `qtypes` +
scoring first, then content authoring, then import, then exam. Deliverable 4's
named test targets span that whole span. **Do you want D4 delivered in that same
phase order — scoring engine complete and green before I touch the publish gate —
or all four test suites written first, then implementations?** I would default to
phase order, because the scoring engine's shape determines the publish gate's
checks, and writing all four suites up front would lock in guesses about
interfaces I have not built yet.
