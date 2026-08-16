# Known issues

Defects that are real, reproduced against the code, and not yet fixed. Each says
what is wrong, how to see it, and why it matters — so the next person to pick one
up does not have to rediscover it.

This file lives in the repository rather than in someone's notes because the
first version of it lived in a scratch directory and was lost when the container
was reclaimed. Findings are only worth having if they outlive the session that
found them.

**This is the defect list. It is not the feature inventory** —
`docs/design/0014-platform-flow.md` §8 is what is not built, and the two are kept
apart deliberately so neither drifts into the other. Something that is wrong
belongs here; something that was never written belongs there.

---

## Outstanding

Re-verified against the code on 2026-08-15. Ordered by what I would fix next.

### 1. An idempotency replay is not scoped to the user

`app/api/deps.py` stores `user_id` on every `idempotency_keys` row and **never
reads it**. `replay()` matches on `scope` and `key` alone:

```python
select(IdempotencyKey).where(IdempotencyKey.scope == scope,
                             IdempotencyKey.key == self.key)
```

So a caller who presents someone else's key with a body that hashes the same
gets that person's stored response body back. The body is attacker-influenced
only in the sense that it has to match, and for `POST /attempts` the body is
small and guessable — an assignment xid. The column to filter on is already
there and already written.

### 2. The invigilation "answered" count disagrees with the marking

It counts `response IS NOT NULL`. A cleared input stores an empty *string*,
which is not SQL NULL, while the scorer treats an empty string as unanswered. A
teacher watching a live sitting sees a student as further along than the marking
will agree they were.

### 3. Progress infers "scored" from the band being non-null

An attempt scored with `band = null` — which happens whenever the band map does
not cover the raw, and is exactly the case a teacher needs to look at — reports
as "submitted" for ever, and the Marking button never appears for it. The
`score_runs` row is the fact to read; the band is a consequence of it.

### 4. A rejected delta is deleted from IndexedDB anyway

The autosave outbox deletes every row in the flushed batch, including the ones
the server named in its rejection list. A `schema_invalid` answer is then gone
from disk, gone from the server, and present only in React state — so it
survives exactly until the tab is reloaded, which is the situation the outbox
exists for.

### 5. The client mints a new idempotency key on every flush retry

`Runner.tsx` calls `attempt.idempotencyKey()` inline at the flush call site, so
each attempt at the same batch carries a fresh UUID and the server cannot
recognise the retry. Starting and submitting hold theirs in a `useRef` and are
correct; it is only autosave. The header is being sent, and it is doing nothing.

### 6. Per-section time limits are authored, gated, shipped — and never enforced

`TestVersionSection.time_limit_seconds` can be set, the publish gate warns when
it is missing (check 19), and `build_snapshot` carries it to the device. The
exam session never reads it: `SessionService` takes its limit from the
assignment or the test version config and writes one attempt-level `expires_at`.
`attempt_sections.expires_at` and `.completed_at` are declared and written by
nothing. A centre that sets 20 minutes on a listening section gets a paper where
that number is displayed and not enforced.

### 7. An item with no answer key disappears from review

Rather than showing as void, it is omitted — so the numbering skips and nothing
says why. A missing key already voids the item at scoring time rather than
failing the student, which is right; the review screen should say so.

### 8. The small-screen guard is cosmetic

The exam shell is hidden below 1024px with `display: none`, but the runner still
mounts and runs its whole lifecycle behind it — clock, autosave, audio grant. A
student who opens a paper on a phone burns their single audio play without
seeing anything.

### Console CSS debt

- `.small` is declared twice, globally, with conflicting meanings — small *text*
  in `styles.css`, a small *chart figure* in `cohort.css` — and whichever loads
  second wins.
- Five tokens have no references: `--good`, `--good-soft`, `--warn`,
  `--warn-soft`, `--r-xl`. There is no success colour in use anywhere.
- Five class names are used in TSX with no rule anywhere: `.panel`, `.paper`,
  `.side__label`, `.q-body`, `.blank`.
- The Bento grid — `.bento`, `.cell`, `.card`, `--span` — is fully specified,
  ships in the bundle, and is referenced by no component.
- Tables have no overflow container, so a wide one forces horizontal body scroll
  below 60rem.
- No modal primitive, no toast, no pagination UI, no icon system, no search.

### Not defects — things that are simply not built

`docs/design/0014-platform-flow.md` §8 is the inventory: no manual band entry,
no marking queue, Writing and Speaking unscored, no image upload (so diagram and
map questions cannot be sat), no self-serve signup, no SMS provider, no delivery
for invitations. Those are absences with reasons, and they live there rather
than here so the two lists do not drift into each other.

## Fixed

In the order they were taken. Kept because the pattern below is the part likely
to recur.

1. The parental-consent check on speaking-slot booking.
2. `POST /orders` billing an org the buyer had no relationship with.
3. Paying an order granting no entitlements.
4. `view`/`assign` content grants reaching nothing.
5. `POST /band-maps` validating nothing, and any org-less account creating the
   platform default.
6. No archive endpoint for passages, questions, groups or audio tracks.
7. `Question.burn_score` hardcoded null.
8. `GET /questions` declaring `q` and never applying it.
9. `GET /content-grants` unable to page.
10. `status` declared and `status_filter` implemented, on the moderation queue
    and on regrades.
11. No listing for `moderation_actions`.
12. `REALTIME_URL` advertising a path the gateway does not serve, and the vite
    dev proxy never forwarding the socket.
13. The snapshot's `media_xid` holding a track xid.
14. A cancelled upload leaving an orphan `processing` track.
15. `consents.doc_hash` stored and never returned.
16. ESLint having no config and not being installed, so `npm run lint` had never
    run for anybody.
17. `consents.revoked_at` — a child-safety control that only switched one way.
    `DELETE /me/consents/{kind}` now exists.
18. **No account could be created at all.** The only path that inserted a `User`
    was one neither client called, so a centre could be built and a paper
    published and not one student could get in. `POST /auth/invite/redeem`.
19. **Seats were structurally dead.** `_seat_licence` required
    `source_kind='seat'` and nothing wrote it, so a centre buying ten seats
    entitled all four hundred of its students. The cause was one unread column:
    `products.kind`.
20. **`Entitlements.consume` had no caller**, so a quantity-bounded grant never
    exhausted. Competition registration now spends an entry and withdrawal
    refunds it.
21. **Three question types could not be published from the console at all** —
    sentence completion and both summary completions. Their payload schema
    requires a `slots` array beside the prose and the form had no field for it.

One gap is recorded elsewhere rather than here because it is a product decision,
not a defect: there is no report-detail endpoint behind the moderation queue.

## The pattern worth naming

Ten of these are the same shape: **a column or field written by one side and
read by neither, or read by one side and written by neither.** The lexicon, the
registry, `device_label`, `deprecated_at`, `hidden_at`, `safety_reports.status`,
`has_attestation`, `under_takedown`, `burn_score`, `archived_at`.

Every one passed every gate this repository has, because every one is locally
correct: the column exists, the write works, the read works, the check compiles.
Nothing tests the seam between them.

`scripts/check_schema_conformance.py` catches the subset where a *declared*
field is never emitted. It cannot catch a column that no contract mentions.

### The gate that now exists

`scripts/check_write_paths.py` is the reverse direction: **every column some
query filters on, that no code path writes.** It runs in `make ci-tests`.

It found five more instances on its first honest runs — `consents.revoked_at`,
`entitlements.revoked_at`, `org_memberships.left_at`, `users.deleted_at`,
`cue_card_sets.archived_at` — all now fixed, all with tests
(`tests/integration/test_unwritten_columns.py`).

Its **second half** asks a narrower question — is this column compared to a
value nothing can produce — and once it could read ORM comparisons rather than
raw SQL alone it found three more:

  * **`visibility` on five of the six asset types.** The column has a
    three-value CHECK on `passages`, `questions`, `question_groups`,
    `audio_tracks` and `cue_card_sets`; `policy.filter_content` ORs four routes
    over it on every listing; and only `PATCH /tests/{xid}` ever wrote it. An
    author could share a whole paper with the platform and could not share the
    passage inside it. `PUT /{asset}/{xid}/visibility` now exists for all five.
  * **`author_private` meant nothing.** `filter_content`'s org route matched any
    visibility, so route 3 — "the actor's own author-private drafts" — could
    only add something for an owner outside the owning org, which does not
    happen. The value was observably identical to `org_private`, and stayed
    hidden because nothing could set it. The org route now excludes another
    author's private drafts. **This is a behaviour change**: a centre admin no
    longer sees a teacher's private draft in a listing.
  * **`speaking_slots.status = 'matching'`.** The batch matcher selected
    `IN ('booking', 'matching')` and nothing ever wrote `matching`. Removed
    rather than written: the matcher runs under an advisory lock, takes
    `FOR UPDATE`, and rolls back on failure, so a slot is never observably
    mid-match and the recovery that state was for cannot arise.

Two things about writing it are worth keeping, because both were wrong first and
both were caught by reverting a known bug and checking the gate noticed:

  * **A `mapped_column(default=None)` is not a write.** The first version
    skipped every column with an ORM default, and all four columns the gate was
    written for are declared `default=None`. It passed while its own motivating
    bugs were reverted. A default is a reason not to expect a write only when it
    supplies a VALUE; `None` is the empty state a lifecycle column starts in,
    which is the precondition for the defect rather than evidence against it.
  * **`row.archived_at = ...` has to be attributed to a model.** A global set of
    assigned attribute names let `Test` cover for `Passage`, `Question`,
    `QuestionGroup` and `AudioTrack`. `Resolver` in that file infers the
    receiver from the `select()` in the expression, from a helper's return
    annotation, and — for `_archive(session, Passage, ...)` — from the call
    sites of the function whose parameter it is.

The lesson under both: **a gate is worth exactly what its calibration proves.**
Reverting each defect one at a time is cheap and it is the only thing that
distinguishes a check from a decoration.

### The same shape, one level up

Three instances now, all found the same way — by asking what re-derives a
declaration, and finding the answer is nobody:

  * **A gate in `make ci` that CI never ran.** `console`, `write-paths` and
    `web-lint` were in the aggregates and in no workflow step. Green, present,
    guarding nothing. `scripts/check_ci_parity.py`.
  * **Documentation nobody re-derives.** `docs/api/student-app.md` is now
    generated by performing the calls, and `tests/integration/test_api_examples.py`
    re-verifies every response body on every run.
  * **The build definition did not build.** Three defects, found in one sitting
    the first time anybody ran `docker compose up --build` on a machine with a
    daemon:

      1. `docker-compose.yml`'s four application services had `build: {context:
         ., dockerfile: Dockerfile}` and **no `target:`**. A multi-stage build
         with no target builds the *last* stage, which here is `caddy` — so the
         api, the worker, the scheduler and the migrate one-shot would every one
         have come up as a web server image and died on `uvicorn: executable
         file not found`.
      2. `ARG NODE_IMAGE` was declared beside `FROM ${NODE_IMAGE} AS web`, where
         it reads best. **An ARG after a FROM is scoped to that stage**; only the
         ones before the first FROM can be interpolated into a later FROM. So the
         file did not parse — `base name (${NODE_IMAGE}) should not be blank` —
         and *nothing* built, including the stages with no interest in Node.
      3. `.dockerignore` excluded `Caddyfile`, which the last COPY in the file
         reads. The error is `"/Caddyfile": not found`, which blames a missing
         file. That same file documents this exact trap for `openapi/`, twenty
         lines above the line that repeated it.

    Nothing caught any of them because nothing here builds an image: CI does not
    (§12 of the CI doc argues the eleven minutes are not worth it), `docker
    compose config` validates compose schema and never opens the Dockerfile, and
    all three defects are **absences** — a key that is not there, a line in the
    wrong place, a pattern in another file. The build definition was read many
    times and looked correct every time.

    `scripts/check_build_definition.py` closes all three, in `make ci-checks`.
    Calibrated by reverting each of the three plus four adjacent mutations, all
    caught, against a passing baseline.

The common factor with the column defects: **two places have to agree and
nothing is looking at the relationship.** The columns are one instance; a
Makefile and a workflow are another; a compose file, a Dockerfile and an ignore
file are three more.

And the second lesson, which cost a launch attempt: **an artefact no test
exercises is an artefact that does not work.** The Dockerfile had been edited,
reviewed and described in three deployment documents without once being built.

### A defect that is a property of the reader's machine

`web/src/features/governance/` held `Exposure.tsx` — the screen — and
`exposure.ts` — `burnPercent`, `rankByBurn` and an `interface Exposure`. Same
for `Takedowns.tsx` and `takedowns.ts`. `App.tsx` imported the screens the way
it imports all forty:

    import { Exposure } from "../features/governance/Exposure";

On Linux that is unambiguous: `Exposure.ts` does not exist, so resolution falls
through to `Exposure.tsx`. On Windows and on a default macOS volume the
filesystem is case-**in**sensitive, `Exposure.ts` *does* exist — it is
`exposure.ts` — and both TypeScript and esbuild try `.ts` before `.tsx`. The
import binds to the wrong module, two screens vanish, and the console does not
start.

It reproduces on every Windows machine and none of ours. CI is Linux, the
production image builds on Linux, the whole suite passes on Linux. **No amount
of testing on the machines we test on could have found it**, and no amount of
reading either — the defect is not in the text, it is in the interaction between
the text and a filesystem property.

`scripts/check_case_collisions.py` (`make case`) is therefore a check that CI
runs *because* CI cannot reproduce it: no two tracked files in a directory may
differ only by case, either in full (git cannot check both out at all) or in the
stem when both extensions are ones a bundler will try. `.css` is excluded on
purpose — a stylesheet import always spells its extension, so `Moderation.tsx`
beside `moderation.css` is not a defect and flagging it would be noise.

The fix was a rename to the convention the rest of the tree already followed —
every other feature names its logic module for what it holds, not for the screen
beside it (`bandMapTable.ts` next to `BandMaps.tsx`, `slotRules.ts` next to
`Slots.tsx`, `money.ts` next to `Billing.tsx`). `exposure.ts` became `burn.ts`
and `takedowns.ts` became `decisions.ts`.
