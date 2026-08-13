# The remaining D3 endpoints

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Continues:** `0006-implementation-phases-5-8.md`
- **Closes:** the "~85 paths not yet built" and the "no `authz.filter()` yet" open
  items from that document.

---

## 0. What is delivered

The contract is now fully served: 149 of 149 operations, checked in both
directions by `scripts/check_api_coverage.py` — nothing promised is missing, and
nothing served is unpromised.

```
spec operations : 149
app  operations : 149
implemented     : 149/149 (100%)

unit         225 passed          (no database needed)
integration  314 passed          (real PostgreSQL, real migrations, real HTTP)
──────────────────────────────
total        539 passed in 4m40s

import contracts   5 kept, 0 broken
openapi            PASS  113 paths / 149 operations / 144 schemas
acceptance         PASS  new question type end to end, DDL fingerprint unchanged
```

| Module | File | Operations |
|---|---|---|
| Central policy | `app/modules/authz/policy.py` | — |
| Auth | `routers/auth.py` | 6 |
| Identity, orgs, cohorts | `routers/identity.py` | 17 |
| Tests, versions, sections, composition | `routers/tests_authoring.py` | 22 |
| Publish gate, key fix, import | `routers/authoring.py` | 6 |
| Asset library | `routers/assets.py` | 27 |
| Exam | `routers/exam.py` | 9 |
| Assignments and regrades | `routers/teaching.py` | 7 |
| Competitions | `routers/competitions.py` | 9 |
| Speaking | `routers/speaking.py` | 10 |
| Registry, media, governance, safety, billing, analytics, realtime | `routers/platform_ops.py` | 36 |

---

## 1. The central policy engine

`app/modules/authz/policy.py` is the answer to "enforce centrally, not with
scattered role checks". The permission matrix from D2 §8 is expressed once, as
data, and it has two halves:

```python
check(actor, action, resource)          # stops a teacher OPENING a rival's test
filter_content(actor, query, model)     # stops it APPEARING IN A LIST at all
```

The second is the load-bearing one. Almost every real multi-tenant leak is a
missing list scope, not a missing detail check — the detail endpoint is the one
people remember to guard. `filter_content` ORs four visibility routes: platform-
global, the actor's organizations, their own author-private drafts, and anything
explicitly shared through `content_grants`.

One deliberate asymmetry in the matrix: `PUBLISH` excludes `TEACHER`. A centre's
reputation rides on its published material, so teacher publishing is opt-in per
organization via `settings.teacher_can_publish` — and that is the only place a
teacher gains a permission they lack by default.

### 1.1 The leak suite

`tests/integration/test_authz_leaks.py` — 54 tests in three layers.

**Layer 1, listings.** Eleven listing endpoints, driven by a rival centre's
teacher holding a valid token, asserting none of the first centre's xids appear
anywhere in the response — walking the whole document, because a leak through an
embedded `current_version` is still a leak.

Each is paired with a positive case asserting the OWNER sees their own row. That
pairing is not decoration: a filter that returns nothing to anybody passes the
leak assertion and breaks the product, and four of these listings initially
passed for exactly that reason — the fixture created no audio track, so
`GET /audio-tracks` was empty for everyone. `home_xids` now enumerates the first
centre's content from the schema rather than from a hand-written list.

**Layer 2, details.** Ten detail endpoints must answer **404, not 403**.
Confirming a resource exists is itself the answer a competitor probing for test
ids is looking for.

**Layer 3, structural.** An AST pass over `app/api/routers/*.py`: any handler
that calls `select(Test | Passage | Question | QuestionGroup | AudioTrack)`
without passing through a scoping helper fails the build. Layers 1 and 2 protect
today's endpoints; this one protects the endpoint someone writes next month. The
exemption list is nine named entries, each with a one-line reason.

**Verified by sabotage.** With `filter_content` short-circuited to return the
unfiltered query, 19 of the 54 tests fail. A leak suite that has never been seen
to fail is a suite that proves nothing.

---

## 2. What the tests caught

Ten classes of defect, spanning twenty-odd endpoints. Every one was found by a
test written to catch that class rather than by reading the code back, which is
the argument for writing them.

### 2.1 Ten endpoints 500'd on every request — `uuid = character varying`

Raw SQL of the form `WHERE xid = :x` bound to `str(xid)`. PostgreSQL has no
`uuid = varchar` operator, so `GET /orders/{xid}`, `/uploads/{xid}`,
`/orgs/{xid}/seats`, `/cohorts/{xid}/progress`, `/cohorts/{xid}/attendance`,
`/test-versions/{xid}/item-analysis`, `/question-types` and the three competition
read endpoints raised `UndefinedFunction` on their first real call.

This is the defect that justifies `tests/integration/test_contract_smoke.py`. It
looks fine in review, passes every unit test, and 500s the first time a user
opens the page. The smoke suite drives all 149 operations with a minimally-shaped
body and asserts only `status < 500`; a 404 or 422 from garbage input is a
correct answer. It found all ten in one run.

### 2.2 A student could read their whole cohort's bands

Three endpoints tested org MEMBERSHIP where they needed a teaching ROLE:
`/assignments/{xid}/progress`, `/cohorts/{xid}/progress`, `/cohorts/{xid}/
attendance`. Every one returns each classmate's live progress, band and
attendance, and every one read `org_id in actor.org_ids` — which is true for the
students too.

This is a different leak from the one `filter_content` addresses, and an org
filter can never catch it: the data is correctly scoped to the organization, and
the person reading it is correctly a member of that organization. It is still
wrong. `TestOrgMembershipIsNotTeachingAuthority` now covers it.

`GET /orgs/{xid}/seats` had the same shape — a student could read how many seats
their school holds and who occupies them — and now requires `MANAGE_ORG`.

### 2.3 `GET /cohorts/{xid}/members` handed classmates each other's phone numbers

`user_dto` carries `phone`. Half a cohort may be fifteen years old. A student
seeing who else is in their class is a roster; a student harvesting their
classmates' phone numbers is a safeguarding incident. Teachers get the full
record, classmates get names only.

### 2.4 `POST /regrades` had no authorization check at all

`GET /regrades` had one. The staging endpoint did not, so any authenticated
student could queue a regrade job against any question version. Caught by a test
written on the assumption it was already guarded.

### 2.5 Numbering: `number_start` was the group's LAST question

`_renumber` walks one row per ITEM, so a five-question group appears five times
and each pass overwrote the group's `number_start`. Section 1 reported starting
at question 3. The fix is three lines; finding it required a test that asserted
the actual numbers rather than that the endpoint returned 200.

### 2.6 Inserting a section 500'd on the unique index

`(test_version_id, position)` is UNIQUE, and there is no reorder-sections
endpoint — so refusing a taken position would leave an author with no way to put
a section in the middle of a test. Insert semantics now shift the occupant and
everything after it.

The shift is two statements, not one: a plain `SET position = position + 1` can
collide with a row it has not moved yet, depending on the order the planner
picks. Parking the affected rows in the negative range first makes it
order-independent. The same applies to group placement and to reorder.

### 2.7 The import template did not import

`GET /imports/template` returned a document in a shape the importer does not
read — `payload`/`key` instead of `text`/`accept`. A template that does not parse
is worse than no template: it teaches a centre that the feature is broken. Both
templates are now asserted to round-trip through `content.importer.parse`.

The CSV **export** had the same defect in the other direction — its own column
set, not the one `from_csv` reads — which made the round-trip claim in the
contract false. It now emits the import columns, and a test exports a published
test and re-imports it.

### 2.8 `QuestionTypeDef` silently dropped `description`

The registry JSON has it, the `question_type_defs` table has a column for it, and
the dataclass did not carry it — so `GET /question-types`, which drives the
authoring UI's type picker, raised `AttributeError`. Adding the field is a Python
change only; the acceptance test confirms the DDL fingerprint is unchanged.

### 2.9 The Click callback read the request body through dead code

`_sync_form` contained `dict(anyio.from_thread.run(request.form)) if False else
_read_form(request)` — a half-finished attempt at reading a form from a sync
handler. Replaced with declared `Form(...)` fields, so FastAPI parses and
validates the callback and the shape is documented.

While rewriting it: the signature was never verified. Click's callback arrives
from an arbitrary IP, so the signature — not an allowlist — is what makes it
trustworthy. `_click_signature` now checks it, and rejected callbacks are
recorded in `payment_events` too, because a provider dispute is settled by
showing exactly what they sent and exactly what we answered.

### 2.10 Two of my own tests were flaky

Both anchored to a module-level `NOW` computed at import. The full suite takes
~5 minutes, so by the time those tests ran, "starts in 60 seconds" had become
"started a minute ago". Reading the clock at call time fixes it. Worth recording
because the failure looked exactly like a real bug in the competition clock.

---

## 3. Decisions worth stating

### 3.1 The competition start is two phases and the payload is encrypted

At T-120s every registered client prefetches the test payload, AES-GCM encrypted,
with a per-client `fetch_after` jitter derived deterministically from
`(competition, user)` — so a client that retries lands in the same slot rather
than rolling the dice again and possibly hitting the spike it was avoiding. At
T-0 each fetches a ~100-byte key.

The encryption is load-bearing, not theatre: an unencrypted payload sitting on
the device from T-120s is a two-minute reading head start for anyone who opens
devtools. `test_the_payload_is_encrypted_and_undecryptable_before_t0` asserts the
ciphertext contains neither the passage title nor the string
`question_version_xid`.

The key is derived from the server secret and the contest's `payload_key_id`
rather than stored, so the key for a contest that has not started exists nowhere
at rest — not in Postgres, not in a backup.

The clock runs from `starts_at`, not from when the client asked. A student on a
slow connection who asks thirty seconds late does not get thirty extra seconds,
and `test_the_clock_runs_from_starts_at_not_from_the_request` pins it.

### 3.2 Age banding is enforced three times

1. `age_band` is a property of the SLOT, so a minor's slot list is filtered
   server-side — it is not a query parameter, and an adult slot is not reachable
   from a minor's client at all.
2. Booking re-checks against the acting user's own age. A client that guesses a
   slot id is still refused. This is the check that makes it an invariant rather
   than a UI behaviour.
3. The live-queue index leads with `age_band`, so a cross-band candidate is not
   merely forbidden in code — it is not returned by the query that finds
   candidates.

A slot's `age_band` defaults to the CREATOR's own band, not to `adult`: a
defaulting mistake must fail closed for minors.

`mixed_supervised` requires a cohort slot with a supervising teacher, and a
`mixed_supervised` slot additionally checks cohort membership at booking.

**A note on the test that nearly did not test anything.** My first version used
the seed fixture's "student", born 2008 — who is eighteen on the fixture's 2026
clock. Every age-band assertion passed against an adult. The suite now creates a
14-year-old and asserts `adult_at > today` in the fixture itself, so it fails
loudly the day that stops being true rather than quietly stopping.

### 3.3 Audio reaches the server only with a report

`POST /speaking/pairs/{xid}/report` is the only path by which conversation audio
is ever stored. The client holds a rolling ~60 s local buffer, discarded unless a
report is filed. Evidence without routine recording of minors' conversations —
defensible under data-protection law in a way mass surveillance is not. Stored
`quarantined`, never `ready`, so it is not servable as ordinary media.

`involves_minor` is set by the SYSTEM from the participants' ages, never by the
reporter — and the test deliberately mislabels the pair record as `adult` to
prove the derivation, not the label, is what counts. Reports involving a minor,
and all grooming reports, are `critical`; everything else is `high`, because if
everything is critical the queue has no order.

### 3.4 A competition regrade is a decision, not a recomputation

`POST /regrades/{xid}/apply` refuses while any affected contest lacks a recorded
decision, returning 409 with the list. `leave_as_is` keeps the published ranking
and records why; `regrade_and_republish` requires a `public_notice` — if a podium
moves, the people on it are told in writing. Either way an `audit_log` row names
the human who chose.

Regrade planning is enqueued through the outbox, not run inline: a popular item
can carry ten thousand sat attempts, and a request that recomputes ten thousand
attempts is a request that times out. The synchronous part is a count, which is
the number an author actually wants first — "how many students does this touch".

`scope.include_competitions` is accepted and ignored. Rejecting it would move the
argument to the client; dropping it makes the rule true regardless of what
anyone sends.

### 3.5 Cloning copies the composition, not the assets

A cloned test references the same passage and question-group versions. A
40-question mock clones in a few hundred bytes, and a deep copy would silently
fork a shared passage so a later correction reached only one of them. Cross-org
cloning requires an explicit `copy` grant — read access is not permission to walk
away with the material.

### 3.6 Assignment targets are materialized at creation

A cohort's membership changes; the assignment's audience does not. Resolving
lazily would mean a student who joins next week is silently late for work set
before they arrived.

### 3.7 Approval does not publish

`POST /test-versions/{xid}/review` with `approved` leaves the version
`in_review`. Approval says the content is ready; publishing stays a separate,
separately audited act. Otherwise the reviewer's click is also a deploy and there
is no moment at which to stop it.

Submitting for review runs the publish gate FIRST and refuses on errors. A
reviewer's attention is the scarcest resource a small centre has, and sending
them a test with nineteen structural errors wastes it.

---

## 4. Still open

> **Read this section as a record, not as current state.**
>
> A documentation audit on 2026-08-13 checked every item below against the code
> and found that most are **closed**. They are left in place because the argument
> for each one is still worth reading and rewriting a design record erases why a
> thing was built — but do not treat an unstruck bullet here as an open task.
>
> Closed since this was written, among others: the realtime gateway is a complete
> WebSocket server, media is genuinely stored and streamed, the audio transcode
> worker exists, the notification transport posts to Telegram for real,
> `discrimination` and `option_distribution` are computed, `publish_version`
> enforces review approval, invites are bound to the phone they were addressed
> to, import sorting is enforced, and the smoke suite runs the scheduler as a
> real process. Operation and file counts quoted anywhere in this document are
> from the date it was written and are now low — `scripts/validate_openapi.py`
> is the live number.
>
> `docs/known-issues.md` is the file that tracks what is actually outstanding.


1. **The workers are not written.** `app/workers/` is empty. Everything that
   should be asynchronous writes to the outbox correctly and nothing drains it:
   the regrade planner, the competition scheduler, the speaking batch matcher,
   the OTP/notification senders, the analytics refresh. The API contract and the
   durable state are right; the daemons that act on them are the next phase.
   → **Closed in `0008-workers.md`.** Audio transcode is the one worker still
   missing, and it is blocked on item 3 (storage) rather than on this.

2. **The realtime gateway is a stub.** `POST /realtime/ticket` mints a ticket and
   returns a URL; there is no WebSocket server behind it, and the ticket is not
   yet written to Redis. Exam timing does not depend on it — `POST /answers`
   doubles as the clock sync — so nothing in the exam path is blocked.

3. **Media is not actually stored.** `POST /audio-tracks` returns an upload
   manifest with an empty `presigned_urls` array; `_sign_grant` still hashes
   rather than HMACs (unchanged from §4 of the previous document). No real audio
   can be served until both are finished.

4. **Item statistics are read but never written.** `/test-versions/{xid}/
   item-analysis` computes p-values live from `item_scores`, which is correct and
   will stay fast for a long time. `item_stats` and `mv_cohort_progress` are read
   by `/content/flagged-items` and `/cohorts/{xid}/progress` and are populated by
   nothing — those endpoints return empty until the analytics job exists.
   → **Closed in `0008-workers.md`.** Both are now written by
   `refresh_analytics`. `item-analysis` still computes live rather than reading
   the projection, so it does not yet show `discrimination` — see §6 there.

5. **`discrimination` and `option_distribution` are placeholders.** Item analysis
   returns `null` and `{}` for both. `p_value` and `common_wrong` are real, and
   `common_wrong` is the one that finds a broken key.
   → **Computed in `0008-workers.md`** (`analytics/stats.py`), and written to
   `item_stats`. The endpoint has not been repointed at the projection yet.

6. **Pagination is a stub.** Every listing returns `next_cursor: null` and a hard
   `LIMIT`. At 1,500 users no library exceeds one page; the field is in the
   contract so adding it later is not a breaking change.

7. **The integration suite is now 4m40s**, most of it the 147-case contract
   smoke. Past the five-minute threshold named in the previous document, so the
   `CREATE DATABASE ... TEMPLATE` change is now due rather than hypothetical.
   → Still open; 5m16s after the worker suites landed.
   → **Closed in `0010-test-suite-speed.md`** — and my diagnosis here was wrong.
   The migration was 5.4 s once; the per-test `TRUNCATE` was the five minutes.

8. **`GET /media/{xid}/content` returns an empty body.** The grant check, the
   content type and the `private, no-store` headers are real and exercised; the
   bytes come from object storage, which is item 3. Per-user tokenized delivery
   means zero CDN cache hits and every byte is origin egress — §7 of the scaling
   document says when that trade flips.
