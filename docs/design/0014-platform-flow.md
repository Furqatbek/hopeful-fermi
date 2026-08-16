# 0014 — The platform, end to end

**Status:** current as of commit `2425e3c`. Every claim here was read out of the
code, and the runtime ones were driven against a live stack (real PostgreSQL,
real API, real browser) rather than reasoned about.

This is the document to hand someone who asks "what actually happens, from a
teacher opening an empty account to a student being told their band". It follows
one paper the whole way:

```
   LIBRARY          PAPER             DISTRIBUTION        SITTING          BAND
   ───────          ─────             ────────────        ───────          ────
   passages    →    test version  →   assignment     →    attempt     →    score run
   audio            + sections        + targets           + answers        + band map
   questions        + groups          + window            + clock          → band
   + answer keys    + band map        + attempt limit     + submit
   groups           → publish         → students see it   → scored
                    → snapshot
```

## How to read this

Every step is marked with what exists **today**, not what is planned:

| Mark | Meaning |
|---|---|
| ✅ | Built and working, server and UI |
| ⚠️ | Partly built — the gap is named on the line |
| ❌ | Not built. The API may exist; the screen does not, or vice versa |

That distinction is load-bearing in this repository. The recurring defect here
is **something declared in `openapi/openapi.yaml` or in a doc, and never
implemented** — or the reverse. A recent example cost real data: `GET
/attempts/{xid}` declared `last_accepted_seq` for months, the handler never
returned it, and as a result every answer a student typed after refreshing the
page was silently discarded. So "it is in the contract" is never evidence here,
and this document does not treat it as such.

§8 is the honest inventory of what is missing. Read it before promising anything
to a centre.

---

## 1. The cast

Authority comes from an `org_memberships` row. There are exactly three DB-checked
roles, plus a separate platform grant:

| Role | Gets it by | In one line |
|---|---|---|
| `student` | accepting an invite | Sits work. Reads own results. |
| `teacher` | accepting an invite | Authors content, sets assignments, runs regrades. |
| `centre_admin` | accepting an invite | Everything a teacher does, plus people, classes, publishing, sharing, band maps. |
| `platform_admin` | `platform_role_grants` | Creates organizations, registers question types, decides takedowns and contest regrades. |

The permission matrix is **data, in one place** — `app/modules/authz/policy.py`
— and never a role check scattered through a handler:

| Action | student | teacher | centre_admin | platform_admin |
|---|:-:|:-:|:-:|:-:|
| `read` | ● | ● | ● | ● |
| `create` / `edit` / `delete` | | ● | ● | ● |
| `import` / `export` | | ● | ● | ● |
| `regrade` | | ● | ● | ● |
| `view_exposure` | | ● | ● | ● |
| `view_answer_key` | | ● | ● | ● |
| `publish` | | ○ | ● | ● |
| `archive` / `share` | | | ● | ● |
| `manage_org` / `manage_band_map` | | | ● | ● |
| `takedown` / `manage_registry` | | | | ● |

○ = teachers may publish only if the centre sets `teacher_can_publish`.

Two of these deserve their names spelled out, because both exist to keep a
**student at your own centre** out of something:

- **`view_exposure`** — item statistics. A p-value tells a student which
  questions to spend their time on.
- **`view_answer_key`** — the answers themselves. `read` cannot express this,
  because `read` admits students by design: a student may read a published
  passage and must never read what it is marked against.

> **Note.** Until `eff8e05`, reading and writing answer keys had no such gate. A
> signed-in student could read the accepted answers for any question at their
> centre, and *any* authenticated account could rewrite the current key of *any*
> question on the platform, cross-org, on published material. Both are fixed and
> covered by regression tests. If you are running an older build, stop and
> upgrade.

### Two different walls

Keeping centre A's material away from centre B, and keeping a *student at centre
A* away from centre A's teacher views, are different problems and use different
mechanisms. Conflating them is easy, because both read `org_id in actor.org_ids`.

- **Tenant isolation** is `policy.filter_content`, applied through `scoped()`.
  Four visibility routes are OR-ed: platform-global, own-org (excluding another
  author's private drafts), own private drafts, and explicit content grants.
  An out-of-scope resource is **404, never 403** — confirming a competitor's
  test exists is itself a leak.
- **Role separation inside one org** is `policy.require`. `scoped()` will not do
  it: it is a *visibility* filter, and every student at the centre passes it.

---

## 2. Stage one — building the library

**Actor:** teacher or centre admin. **Console:** the *Library* group.

Four asset types, each versioned, each org-scoped, each composed into papers **by
reference** — nothing is ever copied. That is what makes a bank a bank, and what
lets one answer-key fix reach every paper that uses the item.

### 2.1 The 17 question types

Question types are **data**, not code: JSON files in `registry/question_types/`,
overlaid by `question_type_defs` rows so a platform admin can register a new type
at runtime without a frontend deploy. Each declares `payload_schema`,
`key_schema`, `response_schema`, `scoring` and an `authoring` block — and the
console's screens are generated from that last one: `authoring.form` is the
question body, `authoring.key_widget` chooses the answer-key editor, and
`authoring.group_form` is what the group carrying the question offers. §2.2a is
what that produces on screen.

All seventeen reduce to **three scoring primitives**:

| Primitive | Types | Marking |
|---|---|---|
| `text_per_slot` | sentence_completion, summary_completion, note_completion, table_completion, form_completion, flowchart_completion, diagram_completion, short_answer | Normalise, then compare to the `accept` list |
| `choice_per_slot` | mcq_single, true_false_notgiven, yes_no_notgiven, matching_headings, matching_information, matching_features, summary_completion_bank, map_labelling | Compare the chosen option id |
| `set_selection` | mcq_multi | Score per correct; over-selection scores zero |

**Every one of the seventeen is Reading or Listening.** There is no Writing or
Speaking question type. This is the single most important fact about the product
and §6 returns to it.

Text answers run through a normaliser chain before comparison — trim, collapse
whitespace, casefold, strip punctuation, flexible hyphens, strip articles, UK/US
spelling, number-words, ordinals, and (for the prose types) contraction
expansion. This is why "thirty" and "30" both mark correct, and why a genuine
missing alternative shows up in item analysis instead of being buried in spelling
noise.

### 2.2 The steps

| # | Step | Endpoint | Console | |
|---|---|---|---|:-:|
| 1 | Create a **passage**, affirming copyright | `POST /passages` | `/passages` | ✅ |
| 2 | Edit the draft | `PATCH /passage-versions/{xid}` | `/passages` | ⚠️ |
| 3 | Publish the passage version | `POST /passage-versions/{xid}/publish` | `/passages` | ✅ |
| 4 | Upload **audio**, affirming copyright | `POST /audio-tracks` → part URLs → `POST /uploads/{xid}` | `/audio` | ✅ |
| 5 | Add the **transcript** | `PUT /audio-tracks/{xid}/transcript` | `/audio` | ✅ |
| 6 | Create **questions** with their answer keys | `POST /questions` | `/questions` | ⚠️ |
| 7 | Create a **question group** carrying the shared rules | `POST /question-groups` | `/groups` | ⚠️ |
| 8 | Put questions into the group | `POST /question-group-versions/{xid}/items` | `/groups` | ✅ |
| 9 | Attach a diagram or map image | `PATCH /question-group-versions/{xid}` | — | ❌ |
| 10 | Set visibility, archive what is burned | `PUT /{asset}/{xid}/visibility`, `POST /{asset}/{xid}/archive` | all four libraries | ✅ |
| — | *Alternative on-ramp:* import an existing bank | `POST /imports` → `GET /imports/{xid}` → `POST /imports/{xid}/commit` | `/import` | ⚠️ |

**Step 2 is ⚠️** because optimistic locking is inert: the ETag is
`"{version_no}-{checksum}"` and `checksum` is set to `""` at creation and never
written again, so two editors both hold `"1-"` and the second silently overwrites
the first.

**Step 6 is ⚠️** because `payload` is not validated against the type's
`payload_schema` on write, only at publish — the contract says otherwise. That
is not a tidiness complaint. It is the mechanism by which a console form that
cannot express a required field still answers 201, and the question sits in the
library looking finished until the gate refuses the paper weeks later. §2.2a is
where that last cost three question types their whole path to a published
paper.

**Step 9 is ❌ for the image, not for the questions.** The slot editors for
`diagram_completion` and `map_labelling` exist and work (§2.2a), so their
questions can now be written; what cannot be attached is the picture they label.
`media.open_upload` supports `kind='image'`, but its only caller passes
`'audio'`, and the registry's own `image_upload` and `hotspot_placer` group
widgets are rendered by nothing. So a diagram question can be authored and still
cannot be sat — the student would be asked to label a diagram that is not there —
and the publish-gate checks written to guard the image remain unreachable.

### 2.2a Authoring without JSON — what a teacher actually types

Until `3d0af3d` the console asked for the question body and the answer key **as
JSON documents**. Creating a matching-headings set meant typing
`{"slots": {"s1": {"accept": ["iv"]}}}` into a textarea, and a group's option
bank meant one line of `A = Living near water` per heading, with the letters
numbered by hand. That is not a thing a centre admin between two classes can do,
and every hand-typed identifier was a duplicate `B` or a skipped `C` waiting to
reach a paper.

Four commits replaced it (`3d0af3d`, `0ef027a`, `859fba0`, `5bfb110`). The forms
are still **generated from the registry**, not switched on `type_key` — a type
registered at runtime must stay authorable, which is the whole reason
`authoring.form` is data — so what changed is the set of widgets that has a real
editor behind it, not the mechanism.

Three ideas carry all of it:

- **Position assigns the identifier.** Nobody types `s1`, and nobody types `A =`.
  A slot list is rows in order; an option bank is one option per line. `A`–`Z`
  for banks, lower-case roman for headings, because that is what a real paper
  does — and a list pasted in *with* its letters already attached is stripped and
  renumbered rather than refused, since pasting eight headings out of another
  window is the actual task.
- **`{{s1}}` in the text derives the `slots` array.** The author writes the line
  as the student will read it; the array the schema demands alongside is computed
  from the markers rather than typed a second time. Two lists that have to agree
  is a bug waiting.
- **The key editor is chosen by `authoring.key_widget`**, and offers five
  controls: accepted alternatives per blank, a fixed picker (True/False/Not
  Given, drawn from the type's own `fixed_options` rather than a second copy of
  the list the scorer matches against), pick-one-from-the-bank, pick-several, and
  raw JSON for a widget nobody has written yet. The picker offers the question's
  own options and keys them by **`id`** — a key holding "Living near water"
  matches nothing a student can submit — while showing the words beside each
  letter, which is what an author chooses with. A type whose bank lives on the
  *group* is the exception: this form does not have the group in front of it, so
  those still take a typed letter, and the screen says so.

**All seventeen types now ask for no JSON anywhere on the question form.** The
last three widgets went in together, because between them they were the only
thing standing between a teacher and a publishable sentence completion:

| Widget | Types | What it is now |
|---|---|---|
| `blank_editor` | `sentence_completion`, `summary_completion`, `summary_completion_bank` | The prose itself, in a box, with **Insert blank (Ctrl+B)** putting the next marker at the caret |
| `audio_timestamp` | `sentence_completion`, `short_answer`, `matching_features` | `m:ss` — or a bare number of seconds — converted to the milliseconds the schema wants |
| `paragraph_picker` | `true_false_notgiven`, `yes_no_notgiven` | A picker of A–Z, which is exactly what `^[A-Z]$` allows |

Two of those were cosmetic. `blank_editor` was not, and it is the defect worth
recording, because nothing on the screen showed it:

> `payload_schema` for those three types requires a `slots` array **as well as**
> the text, and `slots` had no field on the form at all. The JSON fallback
> covered the body and nothing could produce the array beside it, so the console
> could emit only `{"text": "…"}` — which `POST /questions` accepts (step 6,
> above), and which the publish gate then refuses:
>
> ```
> error  PAYLOAD_INVALID  Invalid question content — 'slots' is a required property
> ```
>
> Driven end to end on a live stack, before and after: 201 on the question both
> times, that finding on the paper the first time and not the second. **Three of
> the most common Reading types could not be taken from this console to a
> published paper**, and the only sign of it was a finding on a paper, weeks of
> authoring later.

The array is derived from the markers now, the way the four structured-text
builders already did it — the markers are the single source of truth, and a
blank that exists in the sentence but not in the list is impossible rather than
merely discouraged.

**The answer key follows the body.** A question whose payload declares its blanks
gets one key row per blank, named `Blank 1`, `Blank 2`, and the manual
add-a-blank control disappears. Counting them a second time was asking the author
to restate a decision they had already made, and to restate it wrong: a two-blank
sentence under a one-row key is `KEY_SLOTS_MISSING` at publish. This applies to
every type whose payload carries `slots` — the slot lists as much as the prose.

Two smaller things the registry declares and the console had been ignoring: a
field's written `label` (so it reads "Sentence" and "Information to locate",
not `text` and `statement`) and its `skills` (so the audio cue appears on a
listening question and not on a reading one).

The JSON fallback stays, and should: a type registered tomorrow with a widget
nobody has written yet is then awkward to author rather than impossible, and the
box names the widget it could not render. It is a fallback and not a plan,
though, and `blank_editor` is why — behind a REQUIRED field, or beside a required
field with no form entry at all, it produces a question the write endpoint
accepts and the gate refuses.

**The group is a separate screen and a separate story.** `authoring.group_form`
is read by nothing: `/groups` has its own hand-built editors for the rubric, the
word limit and the option bank. That covers what the group needs except the
`image_upload` and `hotspot_placer` a diagram or map wants — which is step 9,
above, and why those two types still cannot be sat.

### 2.3 Copyright attestation — how the liability is handled

This is a contractual promise, and it is real.

Every passage, every audio upload and every import **must** carry an attestation.
It is a required field, refused rather than defaulted, on the stated grounds that
a missing attestation which quietly becomes "original" manufactures a claim the
uploader never made.

Claims: `original` · `licensed` (requires a `licence_note`) · `public_domain` ·
`permitted_excerpt`. Nothing is pre-selected in the console.

`content_attestations` stores the claim, **the sha256 of the exact statement text
the uploader agreed to** plus its key and version, the request IP, and a hashed
user-agent. Hashing the statement is what makes the record evidence: it proves
what the wording said on the day, not what it says now. The attestation is
recorded even when the import then fails to parse, because handing the file over
is the moment the claim was made.

The publish gate refuses material with no attestation or an open takedown.

**Gap worth knowing:** no attestation is captured for a *question* or a *test*,
though the table permits both. A question stem typed straight out of a Cambridge
paper leaves no evidence row anywhere. Given the stated assumption that some
centres will try exactly this, that is the hole to close first.

---

## 3. Stage two — assembling and publishing a paper

**Actor:** teacher (compose) and centre admin (publish). **Console:**
`/tests` → `/versions/:xid`.

```
Test  ──┬── TestVersion v1 (published, frozen, snapshot materialised)
        └── TestVersion v2 (draft, editable)
                 │
                 ├── Section 1  → passage_version_id | audio_track_id
                 │      ├── group placement → question_group_version_id
                 │      └── group placement → …
                 ├── Section 2 → …
                 └── band_map_version_id   ← how raw becomes band
```

| # | Step | Endpoint | Console | |
|---|---|---|---|:-:|
| 1 | Create the test (also creates draft v1) | `POST /tests` | `/tests` | ⚠️ |
| 2 | Add sections | `POST /test-versions/{xid}/sections` | `/versions/:xid` | ⚠️ |
| 3 | Attach group versions to sections | `POST /sections/{xid}/groups` | `/versions/:xid` | ⚠️ |
| 4 | Reorder | `POST /sections/{xid}/reorder`, `PATCH /sections/{xid}` | `/versions/:xid` | ✅ |
| 5 | Choose or build the **band map** | `GET /band-maps`, `POST /band-maps` | `/band-maps` + inline picker | ✅ |
| 6 | Attach the band map to the version | `PATCH /test-versions/{xid}` | `/versions/:xid` | ⚠️ |
| 7 | **Preview** — sit your own draft | `POST /test-versions/{xid}/preview` | `/versions/:xid/preview` | ✅ |
| 8 | Run the publish gate | `POST /test-versions/{xid}/validate` | `/versions/:xid` | ✅ |
| 9 | Submit for review; someone else approves | `POST …/submit-review`, `POST …/review` | `/versions/:xid` | ✅ |
| 10 | **Publish** — freeze and snapshot | `POST /test-versions/{xid}/publish` | `/versions/:xid` | ✅ |

`kind` ∈ `mock | practice | competition | placement`. `variant` ∈ `academic |
general_training`. Sections accept `reading | listening` only from the API,
though the DB and contract allow all four skills.

**Numbering is derived server-side on every structural write**, one number per
*slot*, test-wide. It is never accepted from the client — accepting it would let
two people editing the same paper produce a test numbered 1–13, 1–13.

Section positions must be `1..n` with no holes: a student entering a section
looks it up by that number, so a hole is a 404 mid-exam.

### 3.1 Band maps — where a band actually comes from

A band map is a table of `{raw_min, raw_max, band}` rows plus a `max_raw`.
`BandMap.band_for` scans it **first match wins**.

```json
{ "max_raw": 40,
  "mapping": [ {"raw_min": 39, "raw_max": 40, "band": 9.0},
               {"raw_min": 37, "raw_max": 38, "band": 8.5},
               {"raw_min": 35, "raw_max": 36, "band": 8.0} ] }
```

The console takes it as lines of `30-32 7.0`.

Four validation rules, all reported at once:

- **`BAND_MAP_GAP`** — every mark `0..max_raw` must be covered. An uncovered mark
  yields `band = null`: a whole cohort given no band, silently.
- **`BAND_MAP_OVERLAP`** — no mark covered twice. First match wins, so an overlap
  makes the later row dead.
- Bands must be in `0.0..9.0` in `0.5` steps.
- `raw_min <= raw_max` on every row.

**Ownership decides who is affected.** A map with `org_id IS NULL` **is the
platform default that every centre inherits** — creating one requires platform
admin. A map with an org is that centre's own curve, and this is the sanctioned
way a centre tunes how strict it is.

Two limits to know:

- **One band map per test version.** `TestVersionSection` has no band-map column.
  Per-skill maps exist only as a `band_maps.skill` *label*: the scorer applies
  the same whole-test curve to every section's raw when computing `per_section`.
  For a Reading-only paper this is correct; for a multi-skill paper it is wrong.
- **A band map cannot be retuned.** ❌ There is no endpoint that creates a new
  `BandMapVersion` or edits an existing one. Every map is permanently
  `version_no = 1`. Retuning means creating a *new map* and re-pointing new test
  versions at it. Existing published versions keep the curve they were frozen
  with — which is right for reproducibility and means "we want to be half a band
  more generous from now on" is a new-map operation, not an edit.

### 3.2 The publish gate

19 checks, run as a **pure function** of the composition tree and the type
registry — no database. Every finding comes back at once with
`{code, severity, path, message, fix_hint, subject_xid}`, deep-linked to the
broken item, and is persisted so the author can close the tab.

Representative checks: `KEY_MISSING`, `KEY_INVALID`, `KEY_SLOTS_MISSING`,
`KEY_SLOTS_ORPHANED`, `PAYLOAD_INVALID`, `BLANK_MARKERS_MISMATCH`,
`OPTION_BANK_MISSING`, `OPTION_BANK_TOO_SMALL`, `KEY_OPTION_UNKNOWN`,
`KEY_OPTION_REUSED`, `WORD_LIMIT_MISSING`, `KEY_EXCEEDS_WORD_LIMIT`,
`BAND_MAP_MISSING`, `BAND_MAP_TOO_SMALL`, `SECTION_COUNT_MISMATCH`,
`ATTESTATION_MISSING`, `TAKEDOWN_OPEN`.

Validate requires **`edit`, not `read`** — findings quote accepted answers
verbatim, and `read` is granted to every student at the centre.

### 3.3 Review, publish, and the snapshot

Review is per-centre and **off by default** (`org.settings.require_review`), on
the grounds that most centres here are one or two people. When on:

- The gate runs *first* — you cannot submit a broken paper for review.
- **Nobody approves their own work**, barred for both the submitter and the
  version's creator, even at a centre that does not require review.
- An approval is over **content**: the recorded fingerprint covers answer keys,
  so approve → edit → publish is refused with `review_stale`.
- Approval is not a deploy. An approved version stays `in_review` until someone
  presses Publish.

Publish runs three gates in cost order — authority, then the 19 structural
checks, then the human one — and then materialises **the snapshot**:
`build_snapshot` resolves the entire student-facing tree into one JSONB document
on `test_versions.snapshot`.

> **Why the snapshot exists.** Serving a paper becomes *one row read* instead of
> a nine-table join, on every request, for every student in a hall. It is also
> what makes a sat paper immutable: the student sees exactly the document frozen
> at publish, whatever happens to the library afterwards.

The snapshot carries **no answer keys and no transcripts**. It is the document
that goes to the device.

Immutability after publish is enforced **twice**: a `_mutable()` check on every
composition endpoint, and a database trigger that refuses any change to
`(title, config, band_map_version_id, total_questions, max_raw, checksum)` on a
published row. Application discipline is not relied upon.

---

## 4. Stage three — getting it to students

| # | Step | Endpoint | Console | |
|---|---|---|---|:-:|
| 1 | Platform admin creates the organization | `POST /orgs` | `/organizations` | ✅ |
| 2 | Centre admin invites people | `POST /orgs/{xid}/invites` | `/centre` | ⚠️ |
| 3 | The invitee gets an account | `POST /auth/invite/redeem` | student `?invite=` | ✅ |
| 4 | The invitee proves their phone | `POST /auth/otp/request` → `/verify` | both SPAs | ✅ |
| 5 | The invitee accepts and becomes a member | `POST /invites/accept` | `/invites` | ⚠️ |
| 6 | Centre admin creates a class | `POST /orgs/{xid}/cohorts` | `/centre` | ✅ |
| 7 | Centre admin puts students in it | `POST /cohorts/{xid}/members` | `/centre` | ✅ |
| 8 | The centre buys a licence | `POST /orders` | `/billing` | ⚠️ |
| 8b | **Or a platform admin grants a trial or seats** | `POST /admin/entitlements` | `/organizations` | ✅ |
| 9 | Centre admin assigns seats | `POST /orgs/{xid}/seats` | `/centre` | ✅ |
| 10 | **A teacher sets the assignment** | `POST /assignments` | `/assignments` | ⚠️ |
| 11 | Targeted students are notified | — (outbox → Telegram) | — | ✅ |
| 12 | The student sees it on their home screen | `GET /assignments` | student `/` | ✅ |

**Step 3 used to be the biggest hole in the product.** The only code path that
inserted a `User` was `POST /auth/telegram/verify`, which neither client calls:
OTP sign-in never registers, and accepting an invitation requires already being
signed in. A centre could be created, a class filled and a paper published, and
not one student could get in.

`POST /auth/invite/redeem` closes it. The **invite** is the authority — issued by
someone with `manage_org`, bound to one number, expiring, stored as a hash — and
the **one-time code** proves the caller holds that number. Neither is sufficient
alone, so a forwarded link is worth nothing: whoever opens it can prove their own
number and gets `invite_not_yours`. The invite is checked *before* the code is
spent, so a bad token cannot burn attempts against a real student's challenge.
`date_of_birth` is required only when registering, because `users.adult_at` is
generated from it and every minor rule reads that column.

The student app opens it from `?invite=<token>` on any path, read **before** the
sign-in gate — a brand-new student has no session, so a route behind that gate
would be the one screen they cannot reach. The token is stripped from the URL on
success.

**What is still missing:** nothing *delivers* the invitation. `create_invite`
returns the raw token to the admin, so the link is passed on by hand. And
`POST /auth/otp/request` sends nothing to a number with no account — deliberately,
so it is not a phone-number oracle. Under `PILOT_OPEN_SIGNIN` the code comes back
in the response and the screen shows it, which is no worse than sign-in: `transport.sms`
has no provider at all, so the pilot flag is currently the only delivery that works
for **anyone**. Before that flag is switched off, `notify` must be able to address
a bare phone, or an invited student cannot receive a code.

**Step 8b** is how a pilot centre is actually switched on. `entitlements.source_kind`
allows five values and exactly one was ever written — `_grant_for_order` hard-coded
`'order'` — so `manual_grant`, `trial`, `promo` and `seat` were declared in the schema
and reachable by no code path, and switching a centre on meant pushing a fake order
through Click or Payme. `POST /admin/entitlements` writes the other four, platform
admin only, with a mandatory reason stored on the row. `order` is deliberately not
accepted: an entitlement claiming a payment must be able to name one. The console
screen is the same panel that already knew how to revoke — its own empty state read
"no seat licence has been granted" while offering no way to grant one.

Note that switching a centre on takes **two** features: `org.assignments` for the
teacher setting the work, and `mock.unlimited` covering each student. The 402 names
the second, which is the mistake an operator makes once.

**Step 9 was ❌ structurally, and this is the fix worth reading.** `_seat_licence`
requires `source_kind='seat'`, and *nothing wrote it* — not the payment path, not the
grant endpoint. So the whole seat subsystem (`_seat_licence`, `assign_seats`,
`release_seat`, `_seat_summary`, the seats screen) read a row no code could produce,
and the per-student coverage check degenerated into "the org holds the feature".

The cause was one unread column. `products.kind` has allowed `'seat_licence'` since
migration 0015 and was SELECTed *nowhere* — `_grant_for_order` wrote `'order'` for
every purchase alike. That single string is the difference between a seat licence and
an org-wide one, because `Entitlements.check` meters an org entitlement per student
only when it reads `'seat'`. **A centre buying ten seats entitled all four hundred of
its students** — precisely the failure `entitlements.py` names in a comment two lines
above the branch: *"without this, buying 10 seats would entitle a 400-student
centre."* The code did exactly that, and the billing suite's own fixture sold a
`seat_licence` and asserted `source_kind == 'order'`.

`_grant_for_order` now reads `products.kind`. Alongside it, `products.features` is
parsed rather than `str()`-ed: the shape migration 0015 writes down in its own table
definition — `[{"feature": "mock.unlimited"}, {"feature": "competition.entry",
"quantity": 4}]` — used to grant a feature literally named
`{'feature': 'mock.unlimited'}`, a valid row against a green order that `check()`
could never match. And `kind` now decides one more thing: a **subscription** grants
no `quantity` at all, because `quantity` means "a consumable balance" and a
subscription's bound is its expiry. Writing `1` there made every monthly subscriber's
`mock.unlimited` a single use, waiting for `Entitlements.consume` to acquire its
first caller and start refusing people who had paid.

A seat granted by hand is held to what the seat subsystem can actually use — an org,
a `SEAT_BUNDLE` feature, and a count — because a seat is a **narrower** grant, not a
stronger one, and one that `_seat_licence` cannot find covers nobody for ever while
reading as granted.

### 4.1 The assignment

`POST /assignments` is where teaching meets billing.

| Field | Values | Notes |
|---|---|---|
| `test_version_xid` | — | **Must be published.** 409 `version_not_published` |
| `target_kind` | `cohort` · `users` · `self_serve` | `self_serve` is declared and broken (§8) |
| `opens_at` / `closes_at` | — | 409 `invalid_window` if closes ≤ opens |
| `time_limit_seconds` | — | **Overrides** the test version's own limit |
| `max_attempts` | default 1 | Counted at `POST /attempts`, **finished attempts only** |
| `mode` | `exam` · `practice` | Genuinely different products — see §5 |
| `allow_review_after` | `never` · `submit` · `close` | Default `close`, because handing a paper back mid-window is how answers travel |

Checks, in order: idempotency replay → window validity → the version exists and
is published → **`policy.require(READ)` on the owning test**, so a rival centre's
org-private paper cannot be assigned → the centre's `org.assignments`
entitlement → per-student seat coverage → write `assignments` +
`assignment_targets` + an outbox event.

**Targets are materialised at creation**, but both read paths *also* admit
current cohort membership, so a student who joins the class next week does see
the work. Removal from a cohort is soft, so targets already written keep
resolving.

**The centre pays when the work is set; the student is never charged.** That is
the whole B2B model in one line, and it is why `POST /attempts` deliberately does
not re-check entitlement on the assigned path.

---

## 5. Stage four — sitting the paper

**Actor:** a student, often a minor, on a laptop in Uzbekistan.
**App:** `student/` — four routes: `/`, `/exam/:xid`, `/result/:xid`.

The governing rule: **the server is the sole authority on time and on scoring.
The client is trusted with neither.**

Every response that could plausibly carry time returns `server_now` +
`expires_at` + `seconds_remaining`, so the client renders a countdown from a
*delta* and never from the device clock — which on a cheap Android handset can be
minutes out. This is also why exam timing needs no WebSocket.

### 5.1 The lifecycle

```
POST /auth/otp/request           →  code (shown on screen in pilot mode)
POST /auth/otp/verify            →  access token + httpOnly refresh cookie
GET  /assignments                →  the home screen
GET  /me/progress                →  the three band tiles
POST /attempts                   →  starts OR RESUMES; idempotent
GET  /attempts/{xid}/payload     →  the frozen snapshot (ETag, 304)
GET  /attempts/{xid}             →  saved answers + last_accepted_seq  ← resume
POST /attempts/{xid}/sections/{n}/enter        →  starts that section's clock
POST /attempts/{xid}/sections/{n}/audio-grant  →  the single play (exam mode)
POST /attempts/{xid}/answers     →  autosave, AND the clock sync
POST /attempts/{xid}/submit      →  idempotent; 30s grace; scored synchronously
GET  /attempts/{xid}/result      →  the band
GET  /attempts/{xid}/review      →  what was right, and why
```

### 5.2 Sign-in

Phone `^\+998[0-9]{9}$` plus a six-digit code.

- **5 codes per phone per rolling hour**, counted in PostgreSQL — deliberately
  *not* in the Redis limiter. That one fails open so a Redis restart cannot end a
  student's exam; this one spends real money and must fail closed. Per phone
  rather than per caller, so an attacker rotating IPs cannot get a fresh
  allowance for each.
- `POST /auth/otp/request` **always** answers 202, even for an unknown number.
  Anything else turns it into a phone-number oracle.
- The refresh token never enters the response body — it is an httpOnly,
  `SameSite=Strict` cookie scoped to `/api/v1/auth`. The access token lives in
  **memory only** in the client.
- In pilot mode (`PILOT_OPEN_SIGNIN`) the code is returned in the body and shown
  on screen, loudly labelled as account-takeover-by-design, because there is no
  SMS contract yet. It disappears the moment the flag is off, with no code change.

### 5.3 Starting, and resuming

`POST /attempts` with an assignment **returns the attempt already in progress**
rather than starting a second one. Resuming is not a new attempt and does not
count against `max_attempts` — only *finished* attempts do.

Resumption is the part that was broken and is worth understanding, because it is
the difference between a refresh costing nothing and costing an hour:

1. `GET /attempts/{xid}` returns the saved `answers` **and the `client_seq` each
   was accepted at**.
2. The client repopulates the fields **and seeds its per-slot sequence
   counters** from those numbers.
3. Without step 2 the counters restart at 1, the server discards any delta not
   strictly above what it holds, and **every answer typed after the refresh is
   rejected `stale_seq`** — invisibly, because a failed flush deliberately shows
   the student nothing.

Restored answers are merged *under* anything already typed, so a slow resume can
never overwrite a keystroke.

### 5.4 Autosave — the load-bearing endpoint

A keystroke reaches **IndexedDB before it reaches the network**. The outbox is
the record; React state is the rendering.

- Batched every **7 seconds**, plus on `visibilitychange` and `pagehide` — the
  moments a session is most likely to be killed without warning.
- **`client_seq` increases per slot.** The server drops anything at or below the
  stored value, so an out-of-order arrival on a flaky link cannot resurrect an
  older answer. This is what makes a blind retry safe.
- **`Idempotency-Key` per flush** — a retry after a timeout replays the stored
  response rather than applying the batch twice.
- **Partially acceptable by design.** One malformed answer must never cost a
  batch of forty; the other thirty-nine belong to a student sitting an exam right
  now. Rejections come back as `{question_version_xid, slot_key, reason}` —
  naming the *question*, because every question in a paper has an `s1`.
- **The response is the clock sync.** Every successful flush carries `server_now`
  and `expires_at`, so the countdown re-anchors continuously for free.

*Verified live:* with the network cut, three answers were typed, held, no alarm
was shown, and all three were accepted on reconnect.

### 5.4a Listening

The player mints the grant **on the student's click**, never on mount — in exam
mode the call succeeds exactly once, so spending it because a component rendered
would burn the play on someone who opened the section to read ahead.

It then **downloads the whole track in one request before playing it**. A grant
lives ~120 seconds and a section is half an hour of audio; handing the grant URL
straight to an `<audio src>` works for about two minutes and then 403s on the
next range request, with no way to re-mint — the section would die mid-sentence
and be unrecoverable. One `fetch()` held open has no such problem, because the
grant is checked when the request *starts*. So the bytes go to a Blob and
playback is immune to the network from then on.

Exam mode renders **no transport controls**: the real test gives none, and a seek
bar is a different exam. If the browser refuses to start playback on its own —
likely on a first visit, since the student's click is several seconds and two
awaits behind by then — an explicit "Start playing" button appears, rather than
leaving a loaded recording with no way to start it and a clock running.

*Verified live:* grant minted, 352 KB streamed (200 full, 206 on range), a
playable WAV decoded, playback running with the top bar's volume applied, and a
second grant refused `409 audio_already_played`.

### 5.4b Review

`GET /attempts/{xid}/review` returns, per slot: the verdict, the points, what
the student wrote, what it normalised to, every accepted answer, which
alternative matched, the normaliser chain that ran, and — for listening — the
transcript at the moment the answer was spoken.

`/review/:xid` renders it against the paper itself, so a student re-reads the
question with their own answer in it rather than a bare list of verdicts. Two
requests: review is marking, the payload is the questions.

**The refusals carry most of the value.** Four things can legitimately withhold
this screen and each is a different sentence with a different thing to do next —
`never` (the centre chose not to release answers), `close` (with the date it
opens), a live competition (with the time it ends), and not-yet-marked. A
student told only "Forbidden" asks their teacher, who asks you.

Two details worth knowing:

- A set answer is reported under the slot key **`"selection"`**, the scorer's own
  name for the whole item, which matches no slot the question declares. The
  client maps it onto the question's first slot — without that, a multi-select
  renders with nothing ticked directly beneath the words "You wrote B, C".
- The transcript is withheld while the same student has a **live attempt** on
  that audio, whoever's review is being read.

### 5.5 Exam mode versus practice mode

These are genuinely different products, and the badge on the card is the only
warning a student gets:

| | Exam | Practice |
|---|---|---|
| Audio | **One play**, enforced server-side. No transport controls | Free replay, with controls |
| Clock | Cannot be paused | Cannot be paused |
| Attempts | Usually 1–2 | Usually more |

### 5.6 Submitting

- The client flushes the outbox **first**, or the last thing typed is never
  marked.
- **A late submit is accepted**, with a 30-second grace window; `late_by_ms` is
  recorded and never penalised. The client must not refuse at +1s and does not:
  the worst outcome of trying is a marked exam, the worst outcome of not trying
  is an unmarked one.
- Idempotent by held key **and** by state — a retry returns the same score run.
- Answers are frozen by a **database trigger**, not by application discipline.
- Marking is **synchronous**: the band exists the moment the student submits.

---

## 6. Stage five — marking, bands, and what a teacher can change

### 6.1 The headline, stated plainly

> **A teacher cannot set a band by hand. There is no marking screen, no rubric,
> no score override and no manual band entry anywhere in the product.**

This was verified by enumerating every path in the API. It is not an oversight to
work around; it follows from the design:

- All 17 question types are Reading/Listening. **There is no Writing or Speaking
  question type**, so there is nothing for a human marker to mark.
- A Writing section cannot even be authored — the API restricts sections to
  `reading|listening`.
- Speaking today is **peer practice, not assessment**: slots, bookings, pairing,
  TURN credentials, ending a pair, and a safety report. No examiner, no band.

If your product plan includes marked Writing, treat it as **unbuilt**, not
partly built. It needs: a writing question type, a long-form answer surface, a
rubric model, a marking queue, a marker role, and a per-criterion band entry
path. None of that exists.

### 6.2 How a band is actually produced

```
score = f(responses, key_versions, band_map_version, engine_version)
```

A pure function. Items dispatch by **scoring primitive**, never by question type.
Awarded points sum into a raw `Decimal`; the raw is rounded to a whole number
(`ROUND_HALF_UP`) and looked up in the band map.

Every pass writes an **immutable `score_runs` row** carrying `engine_version`,
`band_map_version_id`, and the exact `{question_version → answer_key_version}`
map used. That is what makes a mark reproducible, and what makes a regrade a
*recomputation* rather than a mutation.

Two behaviours to know:

- **A missing key voids the item rather than failing the student.**
- **A raw the band map does not cover yields `band = null`**, and the run still
  scores. This is why gap validation matters.

### 6.3 The three levers a teacher does have

| Lever | What it changes | Who |
|---|---|---|
| **The band map** | How raw converts to band, for future papers | centre_admin |
| **The answer key** | What counts as correct, for papers already sat | teacher |
| **The regrade** | Re-applies the above to finished attempts | teacher |

A teacher changes a band **only by changing an input**. There is no direct edit.
Whether that is right for your market is a product decision — but it is the
current behaviour, and every band the system has ever issued is reproducible
because of it.

### 6.4 The teacher's workflow after students submit

| # | Step | Endpoint | Console | |
|---|---|---|---|:-:|
| 1 | Watch the sitting live | `GET /assignments/{xid}/progress` | `/assignments` → Invigilate | ✅ |
| 2 | Read the class result and band distribution | same | `/results` | ✅ |
| 3 | Open one student's marking, see **why** | `GET /attempts/{xid}/review` | `/results` → Marking | ✅ |
| 4 | Diagnose: is the key wrong? | `GET /test-versions/{xid}/item-analysis` | `/item-analysis` | ✅ |
| 5 | Correct the key — stages a regrade in the same request | `POST /question-versions/{xid}/keys` | `/regrades` | ✅ |
| 6 | Or stage a regrade by hand | `POST /regrades` | `/regrades` | ✅ |
| 7 | **Read the impact before anything moves** | `GET /regrades/{xid}` | `/regrades` | ✅ |
| 8 | A finished contest? A platform admin decides | `POST /competitions/{xid}/regrade-decisions/{job}` | `/regrades` | ✅ |
| 9 | Apply | `POST /regrades/{xid}/apply` | `/regrades` | ✅ |

**Invigilation** (step 1) authorises on a *teaching role at the assignment's org*
— deliberately not plain org membership, because the response carries every
classmate's live band. Time left is derived from the server's clock, never the
invigilator's. It polls every 5 seconds; the realtime push described in the
contract does not exist.

**Marking** (step 3) shows per slot: the verdict, awarded/max points, the raw
response, the *normalised* response, the accepted answers, which alternative
matched, and an explain blob naming which normalisers ran. Every staff read
writes an `audit_log` row. Note that `allow_review_after` binds the **student**,
not the staff room — a teacher who set `never` has not blinded themselves to
their own class's marking. But a **live competition on the same paper blocks
review for staff too**, because a contest can be cross-org and a coach reading
the key mid-contest is exactly the leak that gate exists to close.

**Diagnosis** (step 4) is the only diagnosis the product supports: there is no
"this student's band should be a 7" control. A **negative point-biserial
discrimination** — the strongest students getting an item wrong — is the
strongest automated signal of a wrong key in the system. Nothing is flagged below
20 responses, because flagging on noise trains authors to ignore flags.

### 6.5 The regrade flow

Three steps, and the separation is the point:

```
stage (dry run)  →  read the impact  →  apply
```

- **Triggers:** `answer_key_change` · `band_map_change` · `engine_fix` · `manual`.
  `answer_key_change` is deliberately absent from the console's list, because the
  key-fix form stages that one alongside the fix that justifies it.
- **Subjects:** `question_version` · `test_version` · `band_map_version` ·
  `attempt`. All four are now scoped to the caller's own centre.
- **A reason is mandatory** in the console even though the server would accept an
  empty one: it is the only record of why finished exams were rescored.
- **The dry run writes nothing to any score.** It recomputes every affected
  attempt in memory and persists only counts: `attempts_total`,
  `scores_changed`, `bands_changed`, `students_to_notify`, `competition_impact`.
- Status becomes **`ready`, deliberately not `completed`** — a dry run that
  reports itself finished is one a tired admin assumes was applied.
- **`students_to_notify` is `bands_changed` by definition.** Notifying on every
  raw-score wobble trains students to ignore the channel you need for what
  matters.
- **Apply recomputes from scratch**, inserts a *new* `score_runs` row, supersedes
  the old one, rewrites item scores, and sends one deduplicated notification per
  student whose **band** moved. Previous runs are retained, so the student's
  history still shows what they were originally marked.
- The student-facing half is `regraded` + `scored_at` on the result, so a teacher
  can say the band moved because a key was corrected — not because the number
  wandered.
- **A finished competition's ranking cannot be moved by a centre.** It requires a
  platform-admin decision with a mandatory rationale, and republishing requires a
  public notice. Enforced twice, in the domain and at the API. This is not a
  disabled button for centre staff: the centre whose students are ranked is not
  the party to decide whether their ranking moves.

---

## 7. UI guidelines

Two apps, two deliberately different visual languages, one shared stance:
**large-screen first**, and **a control the server would refuse is absent with
the reason written beside it, never disabled**.

### 7.1 The admin console — Bento Grid × Soft Minimalism

**Ground rule:** the page is *not white*. A faintly cool grey ground with white
compartments floating on it is what makes a bento read as separate boxes rather
than ruled sections of one sheet. Pure white on white needs a hard border to show
the seam, and hard borders are what this style avoids.

#### Tokens

```css
/* Colour */
--ground: #f4f5f7;   --surface: #ffffff;   --surface-2: #fafafb;
--ink: #16181d;      --ink-2: #3f4550;     --muted: #6b7280;
--line: #e8eaee;     --line-2: #f0f1f4;

/* One accent, low saturation. Indigo rather than a primary blue,
   because it stays calm over the large flat areas a sidebar creates. */
--accent: #5457d6;   --accent-ink: #ffffff;   --accent-soft: #eeeefc;

--danger: #c0392f;   --danger-soft: #fdeceb;
--good:   #2f8659;   --good-soft:   #e9f5ee;
--warn:   #9a6614;   --warn-soft:   #fdf3e2;

/* Space — a 4px base used as a scale, never by eye. The single biggest
   reason an interface reads as "designed" is that its gaps agree. */
--s1:.25rem  --s2:.5rem  --s3:.75rem  --s4:1rem  --s5:1.5rem  --s6:2rem  --s7:3rem

/* Radius — larger on containers than on the controls inside them, which is
   what makes a compartment look like it holds things. */
--r-sm:.5rem  --r-md:.75rem  --r-lg:1rem  --r-xl:1.25rem  --r-pill:999px

/* Shadow — two very low-opacity layers: a tight one for the edge, a wide one
   for the lift. A single dark shadow is what makes card designs look cheap. */
--shadow:      0 1px 2px rgba(16,18,25,.04), 0 4px 16px rgba(16,18,25,.04);
--shadow-lift: 0 1px 2px rgba(16,18,25,.05), 0 10px 28px rgba(16,18,25,.07);
```

**Typography.** Self-hosted Inter (variable 400–700) in three subsets — latin,
latin-ext, cyrillic — 152 KB total. Self-hosted rather than a CDN because the
audience is on Uzbek mobile data and a third-party font is a third-party outage.
`font-feature-settings: "cv11", "ss01"`. Tabular figures are switched on
**per element** where numbers must line up, not globally, because proportional
figures read better in prose.

Dark mode is `prefers-color-scheme` only — the same two-tone relationship, kept
soft, because near-black would make compartments float on a void. There is no
theme switch: the console follows the operating system, which is why a screen
recorded on a light machine and the same screen on the reader's dark one are not
the same picture.

`color-scheme: light` / `dark` is declared alongside the tokens, and answers a
different question from them. The custom properties restyle **what the stylesheet
draws**; `color-scheme` restyles **what the browser draws for itself** — checkbox
and radio glyphs, the select arrow, scrollbars, the date picker. It was missing
until `7d322c3`, so those stayed in light mode on a dark page: white checkboxes
on `#101216`, the brightest thing on the screen, attached to the quietest control
on it. A token sweep cannot find this, because there is no token to sweep.

#### Layout and navigation

- Persistent **17rem sidebar** + scrolling main column, `height: 100vh`.
- **One breakpoint in the entire stylesheet: 60rem (960px).** Below it the
  sidebar becomes a fixed off-canvas drawer that closes itself on navigation, so
  it cannot cover the page just requested.
- The information architecture is **pure data** in `nav.ts`, rendered as a
  one-open-at-a-time accordion. Groups: Library · Teaching · Reports · My centre ·
  Platform.

Eight IA rules, each pinned by a test:

1. The group holding the current page **opens by itself**, on load and on every
   navigation — including one the sidebar did not start.
2. A **closed** group containing the current page shows a 3px accent dot. That is
   the compensation for the accordion hiding four-fifths of itself.
3. **Never two groups open at once.** The state lives in the click handler, not
   an effect, so it is never briefly wrong.
4. A closed panel gets **`inert`** — `overflow: hidden` alone would leave the
   links keyboard-focusable.
5. `activeFor()` matches **longest-path-first**, then a NESTED table maps
   `/versions*` → `/tests` and `/invites*` → `/account`.
6. The Platform group is **removed entirely, not disabled**, for non-admins.
7. **Writing is deliberately absent** — there are no Writing screens and no
   marking engine. A test fails if anyone adds it.
8. **No group may exceed seven items.** Library sits at the cap.

The accordion animates `grid-template-rows: 0fr → 1fr` rather than `max-height`,
so easing runs against real content height and nothing is clipped. `min-height: 0`
on the child is what permits the collapse. `prefers-reduced-motion` kills it.

#### Screen patterns

All 31 routes are built screens. Every listing is a table.

| Pattern | Shape |
|---|---|
| **List screen** | `.page` → `<h1>` → inline create form → `{error}` → a bare `<table>` that *is* the bento cell → trailing `.muted` caveats |
| **Detail** | Not a route or a modal — the row's action toggles an accent panel **below** the table |
| **Empty state** | One full-width `.muted` row *inside* the same table, with a sentence saying what to do next |
| **Error** | `problemText()` flattens the RFC 9457 document and renders **every** finding, joined by newlines; `.error` sets `white-space: pre-line` so that works. A soft tinted block, never bare red prose |
| **Loading** | Always a sentence, never a spinner — "Loading…", "Working it out…", "Reading the score…" |
| **Permission** | The control is **absent**, with the reason written beside it |
| **Destructive** | Reversible → two-click inline confirm. Irreversible → an acknowledgement checkbox that gates the button ("I have read these numbers") |

**Control rules**, all of them added after measuring rather than looking
(`7d322c3`, `b812c7f`, `3cb5f9a`, `400ebbe`):

- `.row` is a flex line, so `input { flex: 1 }` filled it — **and a checkbox is
  an `input`**. A tick box grew to 40rem with its label stranded at the far end.
  Text fields flex; `[type=checkbox]` and `[type=radio]` are `flex: 0 0 auto` at
  `1rem`, because they are glyphs, not fields.
- A `fieldset` is a **list of choices**, so it is `flex-direction: column` with a
  gap. As a block it gave three toggles nothing but line-height between them, and
  three separate decisions read as one paragraph.
- `.page > label` is `display: block` with the field's own bottom margin after
  it, so a form is label-over-field down the page rather than a run of inline
  words.
- `.link` is `button.link, a.link`. It was written for `<button>` alone, so every
  anchor styled as a link kept button padding, a border and the wrong size —
  visible in tables, where `td a` also has to give up the accent colour.
- A component stylesheet may not name a global token. `--warn` re-declared inside
  `.items` shadowed the global one for everything nested under it; it is
  `--check-key` now. Local names for local meanings.
- A form is a flex **column**, so it stretches its children across the cross
  axis. A bare `.link` — one not inside a `.row` — came out 990px wide with its
  label centred in the middle of the page, reading as a heading rather than
  something you can press. `width: fit-content`, not `align-self: flex-start`,
  which would also un-centre the links that sit in a row beside a full-size
  button.

**Link colour rule.** `a { color: var(--accent) }`, underline on hover only —
but **inside a table cell**, `td a { color: var(--ink); font-weight: 500 }`,
earning the accent only on hover. Forty blue links down a column would make the
most-read cell the loudest thing on the page.

**Numeric rule.** `.num` and any `td:has(.num)` get `tabular-nums`.

> **⚠️ The Bento grid is currently dead CSS.** `.bento`, `.cell`, `.card` and the
> `--span` custom property are fully specified and ship in the bundle, and are
> referenced by **zero** components — every screen is a flat vertical stack. The
> tokens above are real and in use; the grid is not. See §8.

### 7.2 The student app

Two visual languages on purpose:

- **The drill surface** (SignIn, Home, Result) — bento cards, phone-friendly.
- **The exam runner** — an imitation of the real computer-delivered IELTS client,
  specified in `docs/design/0013-student-exam-ui.md`.

> **We copy the interaction design, never the brand.** Familiarity is what
> produces transfer to the real test; brand identity invites a letter. A student
> must find the layout familiar and must never mistake this for the official test.

#### The exam shell

A fixed, non-scrolling three-region grid: `auto / minmax(0,1fr) / auto`.
`min-height: 0` on the middle row is load-bearing — without it the row refuses to
shrink below its content, the panes stop scrolling, and the whole page silently
becomes a scrolling document that loses the fixed timer.

| Region | Contents |
|---|---|
| **Top** | Candidate + section (left) · **timer, centred** · Settings (right) |
| **Body** | Reading: passage left, questions right, draggable divider |
| **Bottom** | Review flag (lower-left) · question palette · "n left" · Finish |

Verified rules from doc 0013:

- The timer is **top-centre** and flashes at **10 minutes** and **5 minutes**.
  Tabular figures, so the width does not jitter as digits change — a clock that
  shifts its neighbours every second is hard to ignore, and this one is already
  deliberately hard to ignore.
- **A flashing clock is exactly the pattern that triggers migraine**, so
  `prefers-reduced-motion` keeps the colour change and drops the flash.
- **Answered questions are UNDERLINED, not filled.** A filled marker and a
  circled one compete for the same glance; an underline sits *under* the shape
  and reads independently of it.
- **Review turns the palette square into a circle.** That is the only thing the
  flag changes.
- Highlighting is select → **right-click** → highlight / clear all.
- **Four** colour themes; "Standard" is black on a pale blue-grey, **not white** —
  a full-white page at exam brightness is fatiguing over three hours.
- **Three text sizes are zoom multipliers** — 1.0 / 1.2 / 1.4 — applied to the
  root font size, with every length in `rem`, so the whole interface scales
  rather than body copy alone.
- **No spell-check** in writing surfaces.
- At zero the paper **auto-submits and keeps the answers**.

#### Student tokens

```css
:root {                        /* Standard */
  --ink:#14171a; --paper:#eef2f8; --pane:#fff; --muted:#5b6570;
  --line:#d5dae0; --accent:#1f5fa9; --highlight:#fff2a8;
  --warn:#b45309; --good:#1d7a4c; --danger:#b42318;
  font-size:16px;
}
:root[data-size="large"]   { font-size:19.2px }   /* ×1.2 */
:root[data-size="x-large"] { font-size:22.4px }   /* ×1.4 */

:root[data-theme="inverse"]         { --ink:#f2f4f6; --paper:#0b0e11; --pane:#101316; --accent:#7db3f0;
                                      --good:#5fc48f; --warn:#f0a44a; --danger:#ff9d94 }
:root[data-theme="cream"]           { --ink:#22201b; --paper:#f3e9d6; --pane:#fbf3e3; --good:#1b6b45 }
:root[data-theme="yellow-on-black"] { --ink:#ffd400; --paper:#000;    --pane:#0a0a0a;
                                      --good:#ffd400; --warn:#ffd400; --danger:#ff9e4d }
```

**Every theme must redeclare every semantic colour, and the two dark ones did
not.** `--warn` and `--danger` were left at the light theme's dark orange and
dark red and carried straight onto a near-black page — measured on `--pane`:
3.71 and 2.83 in `inverse`, 3.94 and 3.01 in `yellow-on-black`, against the 4.5
the rest of the sheet clears. These are not decorative. `--warn` is the timer at
**ten minutes left** and the word limit that decides whether an answer is marked
wrong; `--danger` is the timer at **five minutes** and the "incorrect" verdict on
review. The least readable text on the page was the text that costs a candidate
marks, and `yellow-on-black` is the theme a candidate chooses *because* they
cannot read low contrast. Fixed in `bb8b188` — now 8.98 / 9.32 and comfortably
clear — with `--warn` taking the palette's own yellow there, since this theme is
deliberately monochrome and a stray orange reads as a rendering fault, and
`--danger` staying amber rather than red, because a red that clears contrast on
black is pink and the clock at five minutes is the one thing that must not be
misread.

#### Question rendering

One slot-based renderer. Prose splits on `{{slot}}` so an input sits **inline in
the sentence**, baseline-aligned and sized in `ch` so it scales with the text
setting. The word limit is shown in `--warn` beside the question number and is
**never enforced client-side** — the scorer checks it against the raw answer and
marks an over-limit answer incorrect outright, never truncated.

**Finishing** lives at the far end of the bottom bar, last in the DOM, behind a
dialog that names how many questions are still blank. It used to sit inline under
the answer field, **one Tab away from the gap being typed into** — and tabbing
between gaps is the ordinary way to fill these in, so the key that should move a
student forward ended their exam instead, on Space or Enter, with no
confirmation. Distance in the tab order is the fix; the dialog is the belt to
that braces. The timer reaching zero still submits **without** the dialog: a
modal must never stand between an expired exam and its marking.

---

## 8. What is not built

The honest inventory. Nothing here is speculative — each was confirmed by reading
the code.

### Blocks a paper from being sat at all

- **⚠️ Six of seventeen types have no layout** — note/table/form/flowchart/
  diagram completion and map labelling — and fall back to labelled boxes.
- **⚠️ `matching_headings` answers only its first paragraph**; the payload
  carries up to 14 and the renderer binds one `<select>`.
- **⚠️ The group instruction line is never rendered.** The snapshot carries
  `instructions` per group; nothing reads it. The student never sees "Complete
  the sentences below" — only the derived word-limit badge survives.
- ~~**❌ Three types cannot be published from the console at all** —
  `sentence_completion`, `summary_completion`, `summary_completion_bank`.~~
  **Fixed.** `blank_editor` derives the `slots` array the schema requires from
  the markers in the prose, so the payload the console builds is complete;
  the answer key takes its rows from the same array. Verified end to end, before
  and after (§2.2a).
- **❌ Diagram and map questions cannot be *sat*** — their questions can now be
  authored (§2.2a), but no image can be attached to the group, so the student
  would be labelling a diagram that is not there. `media.open_upload` supports
  `kind='image'` and no caller passes it; `image_upload` and `hotspot_placer` are
  declared in the registry and rendered by nothing.

### Missing screens for endpoints that work

- **❌ No student account, invitations or consents screen.** A student must sign
  into the *admin console* to join their own centre.
- **⚠️ The console's own Invitations screen is orphaned** — built and routed, with
  nothing linking to it.

### Onboarding and billing

- **❌ No self-serve signup.** An account requires an invitation from a centre,
  and an organization requires a platform admin. That is a deliberate shape for a
  B2B pilot, not an omission — but there is no consumer on-ramp.
- **❌ Invitations are never delivered** — the admin passes the link by hand.
- **⚠️ No SMS provider at all.** `transport.sms` raises, so the pilot flag is the
  only working delivery for anyone, and `notify` cannot address a bare phone,
  which invited students need.
- ~~**❌ Seats are structurally dead** (`source_kind='seat'` is never written).~~
  **Fixed.** `_grant_for_order` now reads `products.kind`, so a `seat_licence`
  purchase writes a seat-metered row, and `POST /admin/entitlements` can grant one
  without a payment. See §4.
- ~~**⚠️ `Entitlements.consume` still has no caller**, so a quantity-bounded
  grant never exhausts.~~ **Fixed.** `POST /competitions/{xid}/register` now
  spends an entry, and `DELETE` gives it back: an entry is spent while you HOLD
  a registration. Charged after the capacity check, so a `competition_full` 409
  costs nothing, and only on the transition into `registered`, so a retried
  201 does not bill twice. The entry records which entitlement it was charged
  against (migration 0028) — a refund has to credit the row the unit came off,
  and re-resolving by feature would put it on whichever pack `check` prefers
  today. `mock.unlimited` is still unmetered, but it is unlimited by
  construction, so there is nothing to spend.
- **❌ `target_kind='self_serve'`** creates an assignment with no targets: invisible
  to every student and a 404 at `POST /attempts`.

### Scoring and bands

- **❌ No manual band entry, no rubric, no marking queue** (§6.1).
- **❌ Writing and Speaking are not scored at all.**
- **❌ A band map cannot be retuned** — no endpoint creates a new version.
- **⚠️ Per-section bands apply the whole-test curve** to each section's raw.
- **⚠️ A regrade silently truncates at 5000 attempts**, despite a docstring
  claiming it chunks.
- **⚠️ The impact report never names the students.** The per-attempt deltas are
  computed and discarded; only counts are persisted.
- **⚠️ The regrade notification's direction is computed from the raw score**, so a
  pure `band_map_change` tells every affected student their band went *down*.
- **⚠️ `score_runs.reason` records the literal `'regrade_key'` whatever the
  trigger was**, so the provenance string is wrong even though the `regraded`
  boolean is right.

### Correctness issues worth fixing early

These are defects rather than absences, so they live in `docs/known-issues.md`
with the reproduction for each, and are not repeated here — two lists of the same
eight things is two lists that drift. The shape of them, so you know whether to
go and read it: an idempotency replay that is not scoped to the user, an
invigilation count that disagrees with the marking, a "scored" test that reads
the band instead of the score run, a rejected autosave delta deleted from disk
anyway, a fresh idempotency key on every flush retry, per-section time limits
that are authored and gated and never enforced, an unkeyed item that vanishes
from review, and a small-screen guard that hides the exam runner while it keeps
running.

### Console CSS debt

Also in `docs/known-issues.md`: a `.small` declared twice with conflicting
meanings, five tokens with no references, five class names used in TSX with no
rule anywhere, the Bento grid that ships and is referenced by nothing, tables
with no overflow container, and the absence of a modal, a toast, pagination, an
icon set and search.

---

## 9. How to verify any of this

The claims about the runtime were produced by running it, not by reading it.

```bash
# 1. a database with the schema
createdb livedemo && DATABASE_URL=… alembic upgrade head

# 2. seed one published Reading paper, a student, and two assignments
DATABASE_URL=… python scripts/seed_demo.py

# 3. the API
uvicorn app.api.main:app --port 8010

# 4. the student app, proxying /api at it
VITE_API_TARGET=http://127.0.0.1:8010 npm run dev --prefix student
```

With `PILOT_OPEN_SIGNIN=true` the one-time code comes back in the response body
and is displayed on the sign-in screen, so a browser can drive the real flow end
to end without an SMS provider.

The gates that keep this document from rotting:

| Command | Catches |
|---|---|
| `make spec` (`check_api_coverage.py`) | Contract and implementation disagreeing |
| `pytest tests/integration/test_authz_leaks.py` | Cross-tenant leaks, and any handler selecting content without scoping it |
| `make write-paths` | Columns written by one side and read by neither |
| `make build-def` | compose / Dockerfile / dockerignore disagreements |
| `make case` | Filenames differing only by case |
| `make path-params` | A route declaring a path parameter its handler ignores |
| `make console` | An admin endpoint with no screen, and stale exemptions |
| `make student-test` | Regressions in the app a candidate sits the exam in |
| `make ci-parity` | Gates that exist but CI never runs |

`make student-test` is new in `bb8b188`. The student app had **no gate at all** —
its tests (the clock, the autosave outbox, the marking display, the themes; 101
of them today) ran nowhere, and neither did its build, so a break in the exam
runner would have reached a candidate before it reached anyone else. The target
and the matching CI step went in together, because `ci-parity` fails a target
that CI never runs — which is what keeps this from happening twice.

---

## Related

- `docs/adr/0001-architecture.md` — modular monolith, and why not microservices
- `docs/adr/0002-student-web-client.md` — website not native; httpOnly refresh cookie
- `docs/design/0013-student-exam-ui.md` — the exam interface, verified against official sources
- `docs/api/student-app.md` — the student-facing endpoint contract
