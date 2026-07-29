# Implementation — phases 5–8

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Continues:** `0004-implementation.md` (phases 0–4)
- **Build order:** ADR-0001 §12

---

## 0. What is delivered

| Phase | Contents | Tests |
|---|---|---|
| 5 | Persistence: config, engine, unit of work, ORM models, content repository | (exercised throughout) |
| 6 | HTTP: app factory, RFC 9457 error mapping, auth, idempotency, exam + authoring routers | **25** |
| 7 | Import: canonical JSON contract, JSON/CSV/DOCX adapters, dry-run, commit, diff | **20** |
| 8 | Exam session lifecycle: issue → autosave → submit → score → review, sweeper, play-once | **22** |

```
unit         212 passed          (phases 0-4, unchanged)
integration   67 passed          (real PostgreSQL, real migrations, real HTTP)
─────────────────────────────
total        279 passed in 54s

import contracts   5 kept, 0 broken
openapi            PASS  113 paths / 149 operations / 144 schemas
```

The integration suite skips cleanly when no database is reachable, so the unit
suite still runs on a laptop with nothing installed. Set `TEST_DATABASE_URL` to
enable it; each run creates and drops its own scratch database and applies the
real migrations rather than `create_all` — the point is to prove the models and
the migrations agree.

---

## 1. Four bugs the integration tests caught

None of these were visible to unit tests, which is the argument for the suite.

### 1.1 `alembic.ini` would break on any password containing `%`

`env.py` set the URL via `config.set_main_option()`, which stores it in a
configparser section. Configparser treats `%` as interpolation, so a URL-encoded
socket path (`host=%2Ftmp`) or a production password containing `%` fails at
deploy time with an error that points nowhere near the cause. `env.py` now reads
the environment and hands the URL straight to `create_engine`.

### 1.2 Models sent explicit `NULL` over every database default

48 timestamp columns were declared `mapped_column(default=None)`. SQLAlchemy
treats that as a Python-side default and emits `NULL` in the INSERT, which
overrides `DEFAULT now()` and violates `NOT NULL`. They now use
`server_default=func.now()` with no Python default, so the column is omitted from
the INSERT and the database fills it.

### 1.3 `raise Conflict(msg, code="attempt_expired")` silently did nothing

`DomainError.__init__` took `**extra`, so a per-raise `code` landed in the extras
dict instead of overriding the class attribute — every conflict surfaced as the
useless generic `"conflict"`. `code` is now a named parameter. Three call sites
were affected and all three had tests asserting the specific code, which is the
only reason it was caught.

### 1.4 A replayed idempotent request returned a differently-shaped body

The live response was serialized by pydantic (`...Z`), the stored copy by
`jsonable_encoder` (`...+00:00`). Same instant, different strings — so a client
retrying an autosave got a body that did not match the original, which defeats
the entire point of storing it. Handlers now emit JSON-safe values themselves
via `app/api/dto.py`, making the two identical by construction rather than by
coincidence of which encoder the framework picks.

**`TRUNCATE ... CASCADE` also caught me out**, though in test infrastructure
rather than product code: truncating `users` transitively wiped
`question_type_defs`, because its `created_by` column references `users.id`.
Every test after the first failed on a foreign key. The fixture now re-seeds the
registry after truncation.

---

## 2. Phase 5 — persistence

**One `MetaData` shared across modules, and that is deliberate.** Cross-module
foreign keys are declared as strings and SQLAlchemy resolves them at
mapper-configuration time, which needs one registry. The boundary that matters is
enforced differently: `import-linter` forbids importing another module's `models`
or `repo`, and all five contracts still pass. A shared MetaData does not let
`exam` read `content.models`; it only lets Postgres enforce the FK.

**Only the subset phases 5–8 actually use is mapped.** Media, cue cards, grants,
exposure and takedowns exist in the schema but have no mapper. An unused mapper
is a lie about what the code does.

**`load_composition()` uses a bounded number of queries** — five round trips
regardless of test size. The naive version is 40+ for a mock and shows up
immediately as a slow publish.

---

## 3. Phase 8 — the exam session

All the rules live in `ExamSession`; the HTTP handlers own none of them. Three
worth restating because the integration tests now pin them against a real
database:

**A stale `client_seq` cannot resurrect an older answer.** Retries plus
out-of-order delivery is the normal case on these networks, and without the
sequence check it silently corrupts answers rather than failing loudly.

**One bad delta does not cost the batch.** An unknown slot is rejected
individually into `rejected[]` and the other 39 commit.

**Late submission is accepted and recorded.** Four seconds past the deadline is a
mobile hiccup, not cheating — `late_by_ms` gets the overrun, anti-cheat reads it,
the scorer does not. Saving *well* past the deadline instead triggers the
auto-submit the sweeper would have done.

And the freeze is proven twice over: once through the service (409
`attempt_frozen`) and once by bypassing the service entirely with raw SQL, where
the database trigger refuses.

---

## 4. Phase 7 — import

The canonical JSON is the contract; adapters are replaceable. Everything
downstream — validation, dry run, diff, commit — is tested once and works
identically for all three sources.

**The DOCX adapter parses the template, not arbitrary Word.** It reads the
canonical JSON from a document property the template's macro writes. An ordinary
`.docx` is refused with `DOCX_NOT_TEMPLATE` and a `fix_hint` that says where to
get the template — because that refusal is either a clear message or a support
ticket. Structural OOXML parsing (styles, tables, numbering) slots in behind the
same interface later and nothing downstream changes.

**The importer is registry-driven, which the tests forced.** My first version
hardcoded `{"text": ...}` as the payload shape; `short_answer` wants `question`
and `true_false_notgiven` wants `statement`. It now reads the field name from the
type's `payload_schema` — otherwise question-type knowledge leaks back into a
module that must not have any.

**Slot keys are extracted, never trusted from the file.** A file that declares
its own `slot_keys` could smuggle a mismatch past the publish gate, which
compares key slots against exactly that array.

**Import checks the document can become content; the gate checks the content is
sound.** An accepted answer longer than its own word limit passes import and is
caught at publish — tested explicitly, so the boundary between the two stages
stays where it is meant to be.

---

## 5. Phase 6 — HTTP

Thin handlers over the domain. What the tests prove that unit tests could not:

* **Auth resolves per request**, not from the token. A suspension or role change
  takes effect immediately rather than lingering for the token's 15 minutes.
* **Another user's attempt is 404, not 403** — confirming an attempt exists tells
  a prober something they should not learn.
* **The entitlement gate is one call site.** No entitlement → 402 with the
  feature named; preview needs none.
* **The publish gate returns every finding at once** over HTTP, each with a
  deep-linkable path and a fix hint.
* **Teachers cannot publish** unless the centre set `teacher_can_publish`.
* **A key fix regrades nothing on its own** — asserted by comparing score rows
  before and after.
* **Import commits as a draft**, so it cannot become a publish bypass.

---

## 6. What is still not built

| Not built | Why it is deferred, not forgotten |
|---|---|
| Competitions, speaking, safety, analytics routers | Phases beyond 8. The modules are stubs with live import contracts |
| The other ~85 paths of the D3 contract | CRUD over the same repositories; no new decisions |
| WebSocket gateway | Contract specified in D3 §4; no exam dependency, by design |
| Dramatiq workers and the outbox relay | Events are written transactionally; nothing drains them yet |
| Telegram/SMS delivery | `notifications` rows are written by the regrade planner; no sender |
| Media upload and transcode | Schema and API contract exist; no S3 wiring |
| Payments | The port shape is specified in D3; no provider integration |

---

## 7. Things to push back on

1. **`_regrade_preview` in the HTTP layer counts affected attempts rather than
   computing full impact.** Band changes and rank movement come from the regrade
   planner when the job runs. The endpoint stays fast on an item sat ten thousand
   times, at the cost of a two-step flow. If you would rather see full impact
   inline, it is a job invocation and a slower endpoint.

2. **The integration suite takes ~55 seconds.** Most of that is one Alembic run
   per session. Acceptable now; if it reaches five minutes, snapshot the migrated
   database as a template and `CREATE DATABASE ... TEMPLATE`.

3. **Idempotency records are stored but never swept in-process.** The
   `expires_at` index exists for a nightly job that does not exist yet.

4. **`_sign_grant` hashes rather than signs.** The shape is right and the
   contract is right; it must be replaced with an HMAC over the app secret before
   any real media is served. Marked in the code, not hidden.

5. **No authz `filter()` yet.** The exam endpoints scope by owner directly, which
   is correct for attempts. The content-listing endpoints that need the central
   filter (D2 §8) are among the ~85 paths not yet built — but the leak test suite
   should land with the first of them, not after.

---

## Where this leaves the project

Every architectural claim made across the five deliverables is now exercised by
running code: the registry adds a type with no migration, the gate returns
everything at once, a key fix supersedes rather than edits, scoring is
reproducible from recorded inputs, competitions refuse to re-rank themselves, and
a student's answers survive retries, reordering and a dead connection.

What remains is breadth, not depth — more endpoints over the same repositories,
and the three feature modules that were always scheduled last because they carry
the least MVP leverage and the most risk.
