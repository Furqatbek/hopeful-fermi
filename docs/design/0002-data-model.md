# Deliverable 2 — Data Model

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Depends on:** `docs/adr/0001-architecture.md` (all five decisions confirmed)
- **Canonical schema:** `migrations/versions/*.py` — every column and index carries its
  justification as an inline SQL comment. This document is the map and the rationale.

---

## 0. Summary, and what was actually verified

I ran this. Everything below was executed against a real PostgreSQL 16 instance, not
reasoned about on paper.

| | |
|---|---|
| Migrations | **17**, single linear history, `alembic upgrade head` → clean |
| Round trip | `upgrade head` → `downgrade base` (92 → 1 relations) → `upgrade head` → identical |
| Tables | **80** (77 base + 3 partitioned parents) + 12 monthly partitions |
| Indexes | **286**, of which **68 are partial** — the working set stays small |
| Foreign keys | 174 · Check constraints: 119 · Triggers: 30 |
| Question types seeded | **17 registry rows covering all 19 required MVP types** |
| Scoring primitives | **3** — and that is the whole closed set |
| Lexicon entries | 148 (84 UK/US spelling pairs, 41 number-word forms, units, articles, contractions) |
| Acceptance test | **PASS** — new question type added end to end, schema fingerprint unchanged |

The acceptance test output is reproduced verbatim in §5. The invariant test transcript
is in §10.

---

## 1. Conventions

| Convention | Decision | Why |
|---|---|---|
| Primary keys | `bigint GENERATED ALWAYS AS IDENTITY` | 8 bytes, sequential, good index locality, cheap joins |
| Public identifiers | `xid uuid` on every externally-addressable entity; **the API speaks only `xid`** | `/tests/1234` is a scraping API. Opaque ids are load-bearing for the anti-scrape requirement, not cosmetic |
| UUID generation | UUIDv7 app-side (`uuid6`), `gen_random_uuid()` as the DB default | Time-ordered v7 keeps index locality; the v4 default is the safety net for admin tooling |
| Timestamps | `timestamptz`, always UTC, rendered in `Asia/Tashkent` | UTC+5, no DST, so the only conversion is at the edges |
| Small closed sets | `text` + `CHECK (x IN (...))` | Not PG enums: `ALTER TYPE … ADD VALUE` has transaction restrictions and values can never be removed. A CHECK is dropped and recreated freely |
| Money | `amount_minor bigint` + `currency char(3)` | Tiyin. Click and Payme both transact in minor units; a float or decimal-string round trip through a provider is how you lose money |
| Deletion | Lifecycle states (`archived_at`) for content, `deleted_at` only on `users` | Soft-delete everywhere is a footgun. Content has a real lifecycle; a generic `is_deleted` flag would be a filter everyone forgets |
| `jsonb` | Only where the shape is genuinely open: question payloads, keys, responses, schemas, reports, snapshots | Everything with a fixed shape is a column. jsonb is the flexibility budget and it is spent deliberately |
| Naming | `<noun>` / `<noun>_versions` / `<parent>_<child>` | One pattern, so a table you have never seen is still predictable |
| Migrations | Raw SQL in `op.execute`, single linear history | Partitions, partial indexes, generated columns and triggers do not round-trip through Alembic autogenerate. The migrations must be readable as SQL |

**One trap worth recording, because it cost me a debugging cycle:** Alembic's
`op.execute()` runs the string through SQLAlchemy `text()`, which parses `:name` as a
bind parameter — including inside `--` comments. A JSON example like `{"n":1}` in a
comment becomes bind parameter `1` and the migration fails at runtime, not at import.
Casts (`::date`) are safe. All JSON examples in the migrations therefore use `": "`.
A CI check for this belongs in Deliverable 4.

---

## 2. Module map

Table ownership follows the module boundaries from ADR-0001 §4. No module reads
another module's tables; cross-module foreign keys point only at root entities
(`users.id`, `organizations.id`, `test_versions.id`).

```
platform   outbox · idempotency_keys · notifications · feature_flags
identity   users · organizations · org_memberships · cohorts · cohort_members
           org_invites · otp_challenges · auth_sessions · consents · user_devices
authz      platform_role_grants                       (+ the policy engine, in code)
safety     audit_log(part.) · safety_reports · user_blocks · user_mutes
           moderation_actions · takedown_requests
qtypes     question_type_defs · lexicon_entries
content    media_assets · media_uploads · content_attestations
           passages(+versions) · audio_tracks · transcripts
           questions(+versions) · answer_key_versions
           question_groups(+versions +items) · band_maps(+versions)
           cue_card_sets(+versions)
           tests(+versions) · test_version_sections · test_version_groups
           test_version_validations · content_reviews
           content_grants · item_exposures(part.) · item_exposure_stats
           import_jobs
exam       assignments(+targets) · attempts · attempt_sections · attempt_answers
           attempt_answer_events(part.) · score_runs · item_scores · regrade_jobs
competitions competitions · competition_entries · competition_results
           competition_regrade_decisions · attempt_signals
speaking   speaking_slots · speaking_slot_bookings · speaking_pairs
           speaking_queue_entries
billing    products · prices · orders · payments · payment_events
           payment_reconciliations · entitlements · seat_assignments
analytics  item_stats · mv_cohort_progress · attendance_facts · user_skill_progress
```

### 2.1 Identity and the two tenancy shapes

The requirement was that unaffiliated individuals and organizational cohorts coexist,
and that a student can belong to a centre *and* use the app privately. Two decisions
carry that, and they are the reason it will not need retrofitting:

**Role is a property of membership, not of the user.** There is no `users.role`
column. `org_memberships(org_id, user_id, role)` means a user with zero memberships is
an individual student by definition, and the same human can be a teacher at one centre
and a student at another with no special casing.

**`attempts.org_context_id` separates the two lives of one person.** A student sitting
their teacher's assigned mock produces an attempt with `org_context_id` set; the same
student practising at 1 a.m. produces one with it `NULL`. The centre's analytics filter
on it (`mv_cohort_progress` has `org_context_id IS NOT NULL` in its `WHERE`), so a
centre sees its own cohort's work and never a student's private practice. Without this
column you get one of two bad outcomes: a privacy leak, or a centre dashboard that
silently counts homework the centre did not set.

| Table | One-line reason |
|---|---|
| `users` | Phone-first identity; `date_of_birth` is `NOT NULL` because a nullable DOB makes the minor-matching rule unenforceable |
| `users.adult_at` | Generated column (`dob + 18y`) so age banding is an indexed comparison that cannot drift from the DOB |
| `organizations` | The B2B tenant |
| `org_memberships` | Role lives here, so the two tenancy shapes coexist without a discriminator |
| `cohorts` / `cohort_members` | The unit a teacher assigns to and reports on |
| `org_invites` | Centre admins add students who have no account yet; token is stored hashed |
| `otp_challenges` | SMS/Telegram OTP with per-phone and per-IP rate-limit windows; the code is stored hashed and never logged |
| `auth_sessions` | Opaque, rotating, revocable refresh tokens — a safety ban must kill a live session, which a stateless JWT cannot |
| `consents` | Consent as evidence: who, when, which channel, which document version + hash |
| `user_devices` | Device fingerprints; one fingerprint across many users is the duplicate-account signal |
| `platform_role_grants` | Admin power is a grant with provenance, not a boolean on the user |

### 2.2 Content — the authoring core

| Table | One-line reason |
|---|---|
| `media_assets` | Storage objects as `(bucket, key)`, never URLs, so the S3 provider is swappable |
| `media_uploads` | S3 multipart resume manifest — a teacher on dropping 4G resumes instead of restarting a 40 MB upload |
| `content_attestations` | Copyright affirmation per upload, with the statement hash, IP and identity: evidence, not a checkbox |
| `passages` / `passage_versions` | Reusable reading passages; `blocks jsonb` (not HTML) because auto-lettering, inline blanks and DOCX round-tripping all need addressable structure |
| `passage_versions.paragraph_labels` | Materialized `A..N` so matching-headings validation never walks the block tree |
| `audio_tracks` | Content wrapper over master + delivery `media_assets`; immutable by design because an attempt's audio must never change under it |
| `transcripts` | Segment-form, so post-exam review jumps to the moment; never exposed during an exam |
| `questions` / `question_versions` | Question **identity** is first-class and outlives any test — item analysis and exposure tracking both ask "how does *this item* behave" across every test it appeared in |
| `question_versions.slot_keys` | Materialized blank identifiers so the publish gate compares slots to keys without re-parsing payload |
| `answer_key_versions` | **The table that makes regrade possible without breaking immutability** — see §3.2 |
| `question_groups` / `_versions` / `_items` | Shared instructions, word-limit rule and option bank live at group level, matching how IELTS sets are actually written |
| `band_maps` / `band_map_versions` | Raw→band curve as versioned data, so retuning is a row and every historical score records which version produced it |
| `cue_card_sets` / `_versions` | Speaking prompts authored with the same versioning and visibility as everything else |
| `tests` / `test_versions` | The composition root; `snapshot jsonb` is the highest-value denormalization in the schema (§3.3) |
| `test_version_sections` / `_groups` | Composition by **reference to an asset version** — this is what "reuse without copying" means concretely |
| `test_version_validations` | The publish gate's full findings list, persisted so an author can leave and come back to it |
| `content_reviews` | draft → in review → published |
| `content_grants` | Explicit cross-org sharing; the marketplace seam, since a purchase later creates the same rows |
| `item_exposures` (partitioned) | Per-item "sat how often, by whom, when"; monthly partitions are detached and archived, not deleted |
| `item_exposure_stats.burn_score` | Items burn once they circulate — this is where you watch it happen |
| `import_jobs` | One row spans parse → validate → dry-run diff → confirm → commit, holding the canonical JSON so the commit applies exactly what the author reviewed |

### 2.3 Exam, competitions, speaking, safety, billing, analytics

| Table | One-line reason |
|---|---|
| `assignments` / `assignment_targets` | Cohort or individual targeting with window, time limit and attempt cap |
| `attempts` | `expires_at` is an absolute server deadline the client never computes; `late_by_ms` records overrun for anti-cheat without penalising a mobile hiccup |
| `attempt_sections` | Where play-once is enforced server-side (`audio_locked_at`) |
| `attempt_answers` | Current answer state, upserted by autosave, frozen at submit by trigger |
| `attempt_answer_events` (partitioned) | Lower-fidelity keystroke telemetry for impossible-speed and paste-burst detection; dropped on a 90-day retention schedule |
| `score_runs` | An immutable scoring result recording *exactly* which key versions, band map and engine version produced it |
| `item_scores.explain` | Which normalizers ran and what was compared — the single most useful support tool in the product |
| `regrade_jobs` | Starts as `dry_run = true`; `competition_impact` surfaces rank and podium changes for an explicit decision |
| `competitions.key_freeze` | Keys frozen at start, so a later fix cannot silently re-rank a finished contest |
| `competition_regrade_decisions` | The governance record: a human chose `leave_as_is` or `regrade_and_republish`, with rationale |
| `competition_results_board_idx` | Column order matches the default tiebreak exactly, so the final board is an index scan with no sort node |
| `attempt_signals` | Anti-cheat as detection and evidence, triaged by severity |
| `speaking_slots.age_band` | Age banding is a property of the **slot**, so a minor cannot land in an adult pool by accident |
| `speaking_queue_entries` index | `(age_band, language, joined_at)` — a cross-band match is not merely forbidden in code, it is unreachable by the query that finds candidates |
| `speaking_pairs.evidence_media_id` | Populated only when a report is filed; audio is never routinely stored |
| `safety_reports_minor_idx` | Partial index on `involves_minor` — the separate higher-priority queue, implemented in one line |
| `audit_log` (partitioned) | Append-only, enforced by trigger; the one table with a legal role |
| `takedown_requests.hidden_at` | Soft-hide on receipt, before review; never hard-delete disputed material, because that destroys the evidence too |
| `payments` | A two-phase state machine, because Payme and Click both are; `UNIQUE (provider, provider_txn_id)` is the idempotency anchor |
| `payment_events` | `UNIQUE (provider, idempotency_key)` — the second delivery replays the stored response instead of re-executing the side effect |
| `payment_reconciliations` | The answer to webhooks that arrive *never*: diff the provider's own ledger daily |
| `entitlements` | One index backs every gated action in the product |
| `item_stats.common_wrong` | "38 students wrote *car park*, your key only accepts *carpark*" — finds a broken key automatically instead of waiting for a complaint |
| `mv_cohort_progress` | Materialized view with a unique index, which `REFRESH … CONCURRENTLY` requires; without it the nightly refresh takes `ACCESS EXCLUSIVE` and the B2B dashboard 500s |

---

## 3. The versioning model

### 3.1 Two layers: reusable assets, and compositions that reference them

```
  ASSETS (identity + ownership + visibility)        COMPOSITION
  ─────────────────────────────────────────         ────────────────────────────
  passages ──< passage_versions ◄──────────────────  test_version_sections
  audio_tracks ◄───────────────────────────────────  test_version_sections
  question_groups ──< question_group_versions ◄────  test_version_groups
                            │
                            └──< question_group_items >── question_versions
  questions ──< question_versions ──< answer_key_versions
  band_maps ──< band_map_versions ◄────────────────  test_versions
```

A test version holds **references to specific asset versions**. Pulling an existing
passage into a new test is one `INSERT` into `test_version_sections` with the existing
`passage_version_id`. No copy, no duplication, and editing the passage later creates a
*new* passage version that published tests do not see.

**Why questions are first-class rather than embedded in their group.** A question
inside a group would be simpler, and it is what most exam platforms do. It is wrong
here for two specific reasons you asked for: item analysis (§2.3, `item_stats`) and
exposure tracking (`item_exposures`) both need to ask "how does *this item* behave
across every test it has ever appeared in", and that question is unanswerable if item
identity is a child of a test. The cost is one extra join per group render, which the
snapshot (§3.3) removes anyway.

### 3.2 Why answer keys version separately from questions

This is the single most important structural decision in the model, so it gets its
own section.

Published content is immutable and attempts bind to the exact version sat. An answer
key is part of published content. Fixing a bad key therefore *appears* to violate
immutability — and if you resolve that by bumping the question version, every past
attempt now points at a version whose key you did not use, and regrade becomes
untraceable.

The resolution: the key is a **separate version chain hanging off the question
version.**

```
question_versions (id=42, payload frozen forever)
   ├── answer_key_versions (v1, is_current=false, superseded_at=…)   ← the wrong key
   └── answer_key_versions (v2, is_current=true, reason='key_fix')   ← the fix

score_runs.key_versions = {"42": 17}   ← this attempt was scored with key id 17
```

Consequences that all fall out for free:

* Scoring is a pure function: `score = f(responses, key_versions, band_map_version, engine_version)`.
* Regrade is a recomputation over frozen inputs, not a mutation.
* "Why did this student get 6.5 in March and 7.0 now" is answerable years later.
* `UNIQUE (question_version_id) WHERE is_current` makes "exactly one live key" a
  database fact rather than application discipline. *(Verified — §10, test 3.)*

This is also the concrete reason event sourcing is not needed (ADR-0001 §6): it buys
recomputability and auditability, and three ordinary tables already deliver both.

### 3.3 `test_versions.snapshot`

At publish, the entire resolved student-facing tree is materialized into one jsonb
document: sections, passage blocks, audio references, group instructions, option
banks, numbered questions, payloads.

* Serving a test is **one row read**, not a 7-table join.
* Competition prefetch is a cache fill of a static blob (ADR-0001 §5.6).
* Immutability becomes structural: nothing can change under a running attempt.

It deliberately contains **no answer keys and no transcripts**. Keys are resolved
server-side at scoring time from `answer_key_versions`; transcripts are served only on
the review screen. The snapshot is the thing that goes to the device, so anything
secret must not be in it.

---

## 4. The question-type registry

### 4.1 The table

`question_type_defs (key, version)` carries six pieces of behaviour as data:

| Column | What it drives |
|---|---|
| `payload_schema` | Validates what the **author** writes |
| `key_schema` | Validates the **answer key** |
| `response_schema` | Validates what the **student** submits — the exam engine needs no per-type code |
| `scoring` | A composition over the closed primitive set, plus the normalizer pipeline |
| `validation` | Cross-field rules JSON Schema cannot express |
| `authoring` | **UI hints — the teacher-facing form is generated from this** |

That last row is the one people leave out, and leaving it out makes "no redeploy" a
lie: you would still have to ship frontend code to render the new type's editor. The
authoring form, the key editor and the preview mode are all named in data.

`question_versions` holds a real foreign key to `(type_key, type_version)`, so a
published question can never reference a definition that was deleted or reshaped
underneath it. *(Verified — §10, test 4.)*

### 4.2 Three primitives cover all nineteen required types

| Primitive | Shape | Types |
|---|---|---|
| `choice_per_slot` | N slots, each answered with an option id | `mcq_single`, `true_false_notgiven`, `yes_no_notgiven`, `matching_headings`, `matching_information`, `matching_features`, `summary_completion_bank`, `map_labelling` |
| `text_per_slot` | N slots, free text, normalized then compared to accepted alternatives | `sentence_completion`, `summary_completion`, `short_answer`, `table_completion`, `form_completion`, `note_completion`, `flowchart_completion`, `diagram_completion` |
| `set_selection` | Choose K of N, unordered, partial credit | `mcq_multi` |

**19 required behaviours → 17 registry rows → 3 primitives.** The row count is lower
than the type count because a type used by both Reading and Listening (MCQ, sentence
completion, matching, table completion) is one definition with `skills: [reading,
listening]`, not two.

Where the option source differs, that is a *parameter*, not a new primitive:
`payload.options` (MCQ), `group.option_bank` (matching, word bank),
`section.passage_version.paragraph_labels` (matching information), or `fixed`
(T/F/NG). `unique_options` distinguishes matching headings (each used once) from
matching features (reuse allowed).

### 4.3 The honest boundary between data and code

Restating ADR-0001 §8.3 now that the model exists, because this is the promise you
should hold me to:

| Change | Cost |
|---|---|
| New type composing existing primitives | **One JSON file. No migration, no deploy.** This is every type on your MVP list, and the acceptance test below. |
| New normalizer (e.g. Latin/Cyrillic transliteration tolerance) | ~20 lines + deploy |
| New scoring primitive (e.g. drag-order with adjacency partial credit) | ~30–50 lines + deploy |
| New lexicon entry (a missing UK/US pair) | **One row.** Admin API, no deploy |

I am not going to build a sandboxed DSL to close that last 1%. It would be an
untrusted-code-execution surface maintained by one person, and it would cost the weeks
that belong to the import pipeline.

### 4.4 Normalizer pipeline and word-limit semantics

Applied in this order by `text_per_slot`:

```
trim → collapse_space → casefold → strip_punctuation → hyphen_flexible
     → strip_articles → contraction_expand → spelling_uk_us → number_word → ordinal_digit
```

**The word-limit check runs on the raw trimmed response, before normalization**,
because the rule counts the words the student actually wrote. Encoded IELTS marking
behaviour:

* A hyphenated compound counts as **one** word.
* A contraction counts as **one** word.
* An all-digit token counts against the `allow_number` allowance, not the word count.
* Exceeding the limit marks the answer **wrong outright** — never truncated and
  re-compared.

Tolerance resolves in three layers, most specific winning:
`type.scoring.normalizers` → `group.word_limit` → `answer_key_versions.tolerance`.
`spelling_uk_us` and `number_word` resolve against `lexicon_entries`, which is why
adding a missing variant is a row rather than a deploy.

---

## 5. Acceptance test — a new question type, end to end, no migration

The test adds **matching sentence endings**, a real IELTS Reading type deliberately
excluded from the MVP seed, to a running database that already holds authored content
and a sat attempt. The definition file is
`docs/design/examples/matching_sentence_endings.v1.json`.

The pass condition is strict: a SHA-256 fingerprint of `information_schema.columns` is
taken before and after. If any DDL ran, the fingerprints differ and the test fails.

```
$ python3 acceptance.py

schema fingerprint before : 52f937ddf9e9c3f8
step 1  registry row inserted  : matching_sentence_endings v1
step 2  authored               : group=1 question_version=3 key=4
step 3  attempt sat            : attempt=2 response="D"
step 4  scored                 : verdict=correct awarded=1
schema fingerprint after  : 52f937ddf9e9c3f8

RESULT: registry rows 17 -> 18, DDL changed: False
PASS — new question type added end to end with zero migrations.
```

What happened at each step, and what it proves:

1. **Register.** Drop the JSON file into `registry/question_types/` and run
   `ielts qtypes sync` (in production: `POST /admin/question-types`, platform admin
   only). One `INSERT`. The authoring UI's type picker now offers it, because the
   picker is driven by `question_type_defs` filtered on `skills`.
2. **Author.** A teacher creates a question group carrying the shared ending list in
   `option_bank`, then a question whose `payload` is `{"stem": "The earliest known
   maps were made", "paragraph_hint": "B"}` and whose key is `{"slots": {"s1":
   {"accept": ["D"]}}}`. Both are stored in existing jsonb columns of existing tables.
   **No new table, no new column.**
3. **Sit.** The exam engine stores `attempt_answers.response = "D"` after validating
   it against `response_schema` — a generic JSON Schema check, with no knowledge that
   this type exists.
4. **Score.** The scorer reads `question_type_defs.scoring.primitive`, sees
   `choice_per_slot`, and dispatches to the same function that already scores
   T/F/NG and matching headings. **Zero exam-engine changes.**

The single query that ties it together — note that nothing in it names a question type:

```sql
SELECT d.scoring, qv.payload, ak.key, gv.option_bank, aa.response
FROM attempt_answers aa
JOIN question_versions   qv ON qv.id = aa.question_version_id
JOIN question_type_defs   d ON (d.key, d.version) = (qv.type_key, qv.type_version)
JOIN answer_key_versions ak ON ak.question_version_id = qv.id AND ak.is_current
JOIN question_group_items gi ON gi.question_version_id = qv.id
JOIN question_group_versions gv ON gv.id = gi.group_version_id
WHERE aa.attempt_id = :attempt_id;
```

---

## 6. Publish gate

`test_version_validations.findings` returns **every** problem at once, each as
`{code, severity, path, message, fix_hint}`. The gate reads only published-content
tables, so it is a pure function of the composition and can be re-run any time.

| # | Check | Reads |
|---|---|---|
| 1 | Every question version has a current answer key | `answer_key_versions … WHERE is_current` |
| 2 | Payload validates against `payload_schema` | `question_type_defs` |
| 3 | Key validates against `key_schema` | `question_type_defs` |
| 4 | Key slots == `question_versions.slot_keys` | materialized array, no payload parse |
| 5 | Blank markers in text match declared slots | `validation.blank_markers_match_slots` |
| 6 | Key option references exist in the group's `option_bank` | `question_group_versions` |
| 7 | Word-limit rule present where the type requires one | `group.word_limit` |
| 8 | No accepted answer exceeds its own word limit | key + group — always an authoring mistake |
| 9 | Listening sections have an audio track in `ready` status | `audio_tracks.status` |
| 10 | `max(audio_end_ms) <= audio_tracks.duration_ms` | *"audio shorter than the question span"* |
| 11 | Section item count == `declared_question_count` | `test_version_sections` |
| 12 | Test-wide numbering is contiguous with no gaps or overlaps | `test_version_groups.number_start` |
| 13 | Band map present and covers `0..max_raw` | `band_map_versions.mapping` |
| 14 | Matching-headings option bank exceeds slot count | `option_bank_at_least` |
| 15 | Paragraph references resolve against the passage | `passage_versions.paragraph_labels` |
| 16 | Diagram/map groups have `ready` media and hotspots covering every slot | `question_group_versions.hotspots` |
| 17 | Every uploaded asset carries a copyright attestation | `content_attestations` |
| 18 | Nothing in the test is subject to an open takedown | `takedown_requests` |
| 19 | Section time limits set | `test_version_sections.time_limit_seconds` |

Checks 17 and 18 are mine, not from your spec. 17 makes the attestation
non-bypassable rather than a signup-time formality; 18 stops a centre republishing
disputed material inside a new test while a claim is open.

---

## 7. Regrade

```
1. Author fixes a key on a PUBLISHED question.
   → UPDATE answer_key_versions SET is_current=false, superseded_at=now() WHERE …
   → INSERT answer_key_versions (version_no=n+1, reason='key_fix')
   → INSERT audit_log (action='answer_key.fix', before, after, reason)
   → INSERT outbox  ← same transaction; this is why a regrade cannot be lost

2. Relay creates regrade_jobs with dry_run = true.
   Finds affected attempts:
     question_versions → question_group_items → question_group_versions
                       → test_version_groups  → test_version_sections
                       → test_versions        → attempts
   Computes competition_impact WITHOUT applying anything.

3. Author/admin sees: "142 attempts affected, 138 scores change, 41 bands change,
                       1 competition affected: 12 rank changes, 1 podium change."

4. On confirm, for each affected attempt:
   INSERT score_runs (reason='regrade_key', key_versions={…new…}, is_current=true)
   after flipping the previous run's is_current — enforced by
   UNIQUE (attempt_id) WHERE is_current, so a bug produces an error, not two truths.
   Old runs are retained: the student's score history is preserved, not overwritten.

5. Notify only students whose BAND changed (not every raw-score wobble),
   via Telegram, falling back to SMS. notifications.dedupe_key stops a retried
   job messaging anyone twice.

6. Competitions do NOT auto-regrade. A competition_regrade_decisions row is
   created and a platform admin chooses leave_as_is or regrade_and_republish,
   with a rationale and an optional public notice. Both outcomes are audited.
```

Step 6 is ADR-0001 §8.4 made structural. A leaderboard that changes by itself looks
like fraud, so the schema does not permit it to happen without a named human decision.

---

## 8. Permission matrix

Enforced centrally by `authz.Policy.check()` and `authz.Policy.filter()`, never by
scattered role checks. Students are omitted: they read only what an assignment or
entitlement grants, and never see answer keys before `assignments.allow_review_after`.

| Action | Teacher | Centre admin | Platform admin |
|---|---|---|---|
| Create content | ✅ in own org | ✅ | ✅ (only they may create `platform_global`) |
| Read | own + `org_private` in own org + `platform_global` + `content_grants` | same | everything |
| Edit draft | own drafts; others' drafts only if the centre enables `content.edit_others` (default **off**) | any draft in org | any |
| Submit for review | ✅ | ✅ | ✅ |
| **Publish** | ❌ default — centre setting `teacher_can_publish`, default **off** | ✅ within org | ✅ anywhere |
| Unpublish / archive | ❌ | ✅ own org | ✅ |
| Delete | own **drafts** only | any org draft | any draft |
| Hard-delete published | ❌ | ❌ | ❌ — nobody. Archive only; attempts reference it forever |
| Share (`content_grants`) | ❌ | ✅ to orgs/users; `view`/`assign`/`copy` | ✅ including `public` |
| Set `platform_global` | ❌ | ❌ | ✅ |
| Import (dry-run) | ✅ own org | ✅ | ✅ |
| Commit import | ✅ → lands as **draft** unless they may publish | ✅ | ✅ |
| Export | ✅ content they may read **and** their org owns | ✅ | ✅ |
| Fix an answer key on published content | ✅ own authored items | ✅ org-wide | ✅ |
| Regrade practice / assignment attempts | ✅ own items, own org | ✅ org-wide | ✅ |
| Regrade touching a competition | ❌ — raises a decision | ❌ — raises a decision | ✅ decides |
| View exposure / burn stats | own items | org items | all |
| Edit band maps | ❌ | ✅ org maps | ✅ platform maps |
| Takedown action | ❌ (may report) | ❌ (may report) | ✅ |
| Edit `question_type_defs` | ❌ | ❌ | ✅ |

Three defaults are deliberate and I would argue for each:

* **Teachers cannot publish by default.** A centre's reputation rides on its material.
  The centre opts in per-org if it trusts its staff.
* **Import commits as a draft** for anyone who cannot publish, so bulk import can never
  become a publish bypass.
* **Nobody can hard-delete published content**, including you. Attempts reference it,
  and a takedown needs the evidence preserved (`hidden_at`, not `DELETE`).

**On row-level security:** I considered Postgres RLS for org isolation and am not
recommending it at MVP. It is genuine defence in depth, but it costs `SET LOCAL` per
transaction, interacts awkwardly with pooling, and makes debugging harder — real
ongoing tax for one person. The cheaper control that catches the actual bug class is a
**leak test suite**: for every content-returning endpoint, assert org B cannot see org
A's content. Add RLS when you have a second engineer or a compliance audit asks for it;
the `visibility` / `org_id` columns are already shaped for the policies.

---

## 9. Multi-tenancy and content isolation

Three scopes on every content asset, `org_private` as the **column default**:

| Scope | Visible to |
|---|---|
| `author_private` | the author only (drafts) |
| `org_private` | **default** — members of the owning org |
| `platform_global` | everyone; only a platform admin can set it |

Plus `content_grants` for explicit exceptions, which is also the marketplace seam.

The contractual promise that a centre's material never reaches a competitor is carried
by three things working together:

1. **The column default.** Nothing becomes shareable by forgetting a flag.
2. **`Policy.filter()`, not just `Policy.check()`.** `check` stops a teacher *opening*
   a competitor's test; `filter` stops it appearing in a list response at all. Almost
   every real multi-tenant leak is a missing list-scope, not a missing detail-check.
   Every query returning content passes through `filter` or it does not ship.
3. **Opaque `xid`s.** Sequential ids would let anyone walk the content library
   regardless of how good the filter is.

---

## 10. Invariants the database enforces

Application code can be bypassed by a migration, a background job, or a 3 a.m. `psql`
session. These are enforced in the database. Transcript from the verification run —
each `ERROR` is the invariant firing correctly:

```
1. generated adult_at  -> Aziza=2028-03-01/minor=true Bekzod=2017-06-15/minor=false

2. ERROR: table public.audit_log_2026_07 is append-only        (UPDATE rejected)
   ERROR: table public.audit_log_2026_07 is append-only        (DELETE rejected)

3. ERROR: duplicate key value violates unique constraint "answer_key_versions_current_uq"
   key versions -> total=2 current=1                (key-fix flow works correctly)

4. ERROR: insert or update on "question_versions" violates foreign key constraint
          "question_versions_type_key_type_version_fkey"       (unknown type rejected)

5. ERROR: published test_version 1 is immutable; create a new version
   ERROR: published test_version 1 cannot return to draft
   published tv status -> archived                  (archiving still permitted)

6. ERROR: attempt 1 is submitted; answers are frozen
   frozen answer -> "A"                             (unchanged after the attempt)

7. ERROR: duplicate key value violates unique constraint "score_runs_current_uq"
   score runs -> total=2 current_raw=31.00          (regrade supersedes correctly)

8. default visibility -> passages=org_private tests=org_private
```

| Invariant | Mechanism |
|---|---|
| Audit log is append-only | `BEFORE UPDATE OR DELETE` trigger on the partitioned parent (PG 13+ cascades to partitions, including future ones) + `REVOKE UPDATE, DELETE` from the app role in production |
| Exposure log is append-only | same trigger function |
| Published test versions are immutable | `test_versions_immutable()` — blocks field edits and status regression, allows archiving and the snapshot backfill |
| Answers freeze at submit | `attempt_answers_frozen()` on INSERT/UPDATE/DELETE |
| Exactly one current answer key per question version | `UNIQUE (question_version_id) WHERE is_current` |
| Exactly one current score per attempt | `UNIQUE (attempt_id) WHERE is_current` |
| Questions cannot reference an unknown type | FK on `(type_key, type_version)` |
| `max_attempts` cannot be exceeded | `UNIQUE (assignment_id, user_id, attempt_no)` |
| A user cannot be paired with themself | `CHECK (user_a_id <> user_b_id)` |
| Duplicate payment cannot be created | `UNIQUE (provider, provider_txn_id)` |
| A webhook cannot be processed twice | `UNIQUE (provider, idempotency_key)` |
| Age can never drift from date of birth | `adult_at` generated column |

---

## 11. Migrations

Single linear history. `NNNN_<module>_<what>.py`, raw SQL, reversible.

| # | Migration | Contents |
|---|---|---|
| 0001 | `platform_extensions` | `pgcrypto`, `pg_trgm`, `citext`, `btree_gin`; `set_updated_at()`, `forbid_mutation()` |
| 0002 | `platform_core` | outbox, idempotency keys, notifications, feature flags |
| 0003 | `identity` | users, orgs, memberships, cohorts, invites, OTP, sessions, consents, devices |
| 0004 | `authz_audit` | platform role grants; partitioned append-only audit log |
| 0005 | `qtypes` | **the registry** + tolerance lexicon |
| 0006 | `media` | media assets, resumable uploads, copyright attestations |
| 0007 | `content_assets` | passages, audio, transcripts, questions, keys, groups, band maps, cue cards |
| 0008 | `content_tests` | tests, versions, sections, group placements, publish-gate results, reviews |
| 0009 | `content_governance` | content grants, partitioned exposure log, burn stats |
| 0010 | `content_import` | import jobs |
| 0011 | `exam` | assignments, attempts, autosave, telemetry, score runs, item scores, regrade |
| 0012 | `competitions` | competitions, entries, results, regrade decisions, anti-cheat signals |
| 0013 | `speaking` | slots, bookings, pairs, live queue |
| 0014 | `safety` | reports, blocks, mutes, moderation actions, takedowns |
| 0015 | `billing` | products, prices, orders, two-phase payments, events, reconciliation, entitlements, seats |
| 0016 | `analytics` | item stats, cohort progression MV, attendance, skill progress |
| 0017 | `seed_registry` | 17 question types, 148 lexicon entries, default band maps |

**0017 is the last time a question type appears in a migration.** Everything after is
an `INSERT`.

Two operational notes from running it:

* **Partition creation is a monthly cron job.** Every partitioned table also has a
  `DEFAULT` partition, so a missed cron run means rows land somewhere recoverable
  rather than an insert failing mid-exam. Seeded through 2026-09.
* **`0017`'s downgrade deletes only *unreferenced* definitions.** My first version
  deleted all builtin rows and hit the `question_versions` FK, which was the FK doing
  its job: downgrading a seed is not a reason to orphan every attempt ever sat.

---

## 12. Things I decided that you should push back on if you disagree

1. **`attempt_answers` is mutable during the attempt, frozen at submit** — not
   append-only per keystroke as ADR-0001 loosely said. What the pure-function property
   actually needs is an immutable *submitted* response set. Per-keystroke immutability
   would mean ~10 M rows/year of history to serve a forensic need that
   `attempt_answer_events` covers more cheaply and on a retention schedule. If you want
   full keystroke immutability for disputes, say so and I will make the events table
   authoritative instead of a sidecar.

2. **`questions` are first-class, which costs a join.** Justified by item analysis and
   exposure tracking (§3.1). If you decided item analysis is not a launch feature, the
   simpler model is questions embedded in groups — but retrofitting item identity later
   means you lose all historical statistics, so I would not take that trade.

3. **No RLS.** §8. I think a leak test suite is the better spend for one engineer, and
   I have said when to revisit.

4. **`date_of_birth` is `NOT NULL` and I collect it.** It is the minimum needed for the
   18 boundary, and the boundary is the safety invariant. I collect nothing else
   sensitive: no PINFL, no passport, no address. If a school demands student ID
   numbers, push back — holding them turns a minor incident into a major one.

5. **`competition_results` is a real table, not just Redis.** The live board is a Redis
   ZSET, but the final board is durable, ranked and reproducible. A leaderboard that
   evaporates on a Redis restart is the kind of thing that ends a paid competition.

6. **17 rows for 19 types.** If you would rather see one row per named type for clarity
   in the teacher-facing picker, that is a labelling concern and I would solve it with
   a display alias rather than duplicate definitions.

---

## Deliverable 2 ends here.

Nothing blocks Deliverable 3 (the API contract). Confirm or push back on §12, and tell
me if you want the OpenAPI spec split by module or delivered as one document — I would
default to one document with tags per module, since a single file is what feeds a
client generator.
