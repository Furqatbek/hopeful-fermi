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

**Nothing, of the defects recorded here.** All seven were fixed on 2026-08-18,
each with a test calibrated by reverting the fix and watching it fail. They are
listed under *Fixed* below with what each one cost.

This section will fill up again. The point of the file is that it is written
down when it does.

### Console CSS debt

Resolved, except where noted:

- ~~`.small` declared twice, globally, with conflicting meanings~~ — the chart
  figure is `.chartlet` now. Two global declarations of one name in two
  stylesheets meant the winner was decided by Vite's module graph.
- ~~Five tokens with no references~~ — all five are in use, and each was
  covering for something wrong. `--good` existed while success was painted in
  the ACCENT, so "that worked, here is your invite token" looked like a link;
  `.issued` is `--good-soft` now, on ten screens. `--warn` existed while the
  checkbox that ends every session a person has open looked exactly like "a
  hyphenated pair counts as one word"; that is `.choice--grave`. `--r-xl` is on
  the preview sheet, the largest container the console draws.
- ~~Five class names used in TSX with no rule anywhere~~ — all five have rules.
  The two that mattered were in the PREVIEW: `.q-body` and `.blank` are a
  question's prose with its answer boxes inline in the sentence, which is the
  whole layout decision of the student runner. Measured in a browser, an
  unstyled blank was **245px** wide sitting mid-sentence; it is 12ch now, as it
  is for the student.
- ~~The Bento grid is dead CSS~~ — **deleted.** `.bento`, `.cell`, `.card`,
  `.cell--quiet`, `.cell--accent` and `--span`: twenty lines shipping in every
  bundle, referenced by zero components since the day they were written. Every
  screen here is a vertical stack of full-width readings and none ever wanted
  two things abreast. The bento IDEA — discrete rounded compartments — is
  carried by the table, by `.panel` and by `.paper`; the grid was an answer to a
  layout problem these screens do not have. A design system that declares what
  it does not use teaches the next person that its declarations are decorative.
- ~~Tables have no overflow container~~ — **fixed**, and it did need the 46
  wrappers rather than a clever rule: `overflow-x` on a table requires
  `display: block`, and a table that is a block is not a table any more — the
  cells stop sharing column widths and the layout the header promises is gone.
  Measured at 700px: `/questions` holds a 782px table inside a 668px box, and
  the page's own horizontal overflow is **0** where it used to carry those
  114px. Scrolling the page sideways moves the header and the sidebar with it,
  so the thing you were trying to read leaves the screen along with everything
  else.
- ~~No pagination UI~~ — **fixed, and it was the smaller half of the problem.**
  The console consumed `next_cursor` NOWHERE; the only mentions of it in the
  codebase were comments explaining that it was always null. Underneath, nine
  listings declared the envelope and one issued a cursor. See *Fixed* below.
- ~~No search~~ — **fixed** on the two listings whose endpoints implement `q`,
  which they had done since they were written while no screen sent it. It
  matters more after paging, not less: a page is 25 now, and finding one item in
  a library of two hundred by pressing "Show more" eight times is worse than the
  limit it replaced.
- **No modal primitive, no toast, no icon system** — **not built, deliberately,
  and this is the reasoning rather than a deferral.** This console's own
  documented patterns are the opposite of all three: a detail view is "an accent
  panel BELOW the table, not a route or a modal"; a result is an inline
  `.issued` block or an `.error` block, which stays on screen and can be
  re-read, where a toast is a message that disappears while you are still
  reading it; and every control is a word, because "Where used" and "Billing"
  say what they do and a glyph needs a legend.

  Building the three would mean writing primitives no screen uses, which is
  exactly the Bento grid — twenty lines shipping in every bundle, referenced by
  nothing, for long enough that it became a documented defect. The right time to
  write a modal is the day a screen needs one.

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
22. **An idempotency replay was not scoped to the user.** `replay()` matched on
    `(scope, key)` and never read the `user_id` it had been storing since 0002,
    so a caller presenting somebody else's key with a body that hashed the same
    was handed that person's stored response — for `attempts.start`, another
    student's attempt. Two changes, and neither is correct alone: the lookup is
    scoped, and migration 0029 widens the unique index to
    `(scope, key, user_id) NULLS NOT DISTINCT`, because a scoped lookup under a
    platform-wide index turns the leak into a denial — the second caller misses
    the replay, executes, and collides on insert. Both halves calibrated by
    reverting each one separately and watching the tests fail.

23. **The invigilation "answered" count disagreed with the marking.** It counted
    `response IS NOT NULL`; a cleared box stores an empty JSON string, which is
    not SQL NULL, and the scorer reads it as unanswered. `has_response` and
    `answered_sql` are one rule now, held together by a seam test that caught
    them disagreeing on its first run — `btrim` with no second argument strips
    spaces only, so `"\n"` was answered in SQL and blank in Python.
24. **An attempt scored with no band read as "submitted" for ever**, so the
    Marking button never appeared for exactly the cohort whose band map has a
    hole in it. The state reads the score run rather than the band.
25. **An item with no answer key vanished from review.** The comment said "marked
    void rather than incorrect"; the code emitted no ItemScore at all.
    `Verdict.VOID` was in the contract and handled by the student's display,
    produced by nothing. Both persistence paths keyed the question id and the
    key-version id off `key_versions`, which by definition has no entry for a
    void item.
26. **A refused answer was deleted from IndexedDB with the accepted ones**, so a
    delta the server declined was gone from disk, gone from the server, and in
    React state only. It is marked rather than deleted — not re-queued, because
    none of the three refusal reasons can ever be accepted by sending the same
    bytes again.
27. **Every retry of an autosave flush carried a fresh idempotency key**, minted
    inline at the call site, so the server could not recognise a retry as one.
    Bound to the batch now, and replaced when the batch changes — reusing a key
    for a batch that gained a keystroke is a 409 the client would have stalled
    on for ever.
28. **The small-screen refusal was a stylesheet.** CSS hides things rather than
    stopping them: below 1024px the runner stayed mounted, started the attempt
    and spent the one audio play a listening section allows. Driven in a browser
    at 800px: two POSTs before, none after.
29. **Per-section time limits were authored, gated, shipped and never enforced.**
    `expires_at` and `completed_at` were declared in the schema AND in the
    contract's own `AttemptSection`, written by nothing. The seeded fixture has
    declared 1200 seconds on its section since it was written.

30. **Nine listings promised a cursor and one issued it.** `{items, next_cursor}`
    is the envelope this contract declares for every paged listing, and the
    `cursor` query parameter was already declared on eight of the ten endpoints
    — so the client could send it and the server ignored it. `next_cursor` came
    back null under a default `limit` of 25: a centre with four hundred students
    had twenty-five of them, and nothing anywhere said the rest existed. The
    console coped by asking for a bigger number — the roster asked for 200 —
    which truncates at whatever somebody guessed.

    Four of them had **no ORDER BY at all**, so which twenty-five was whatever
    the plan emitted. Keyset rather than offset: an offset shifts when a row is
    added or removed while you read, which on a roster being edited is a student
    who silently never appears. `app/api/paging.py`, and a walk-to-the-end test
    that asserts every row exactly once.
31. **`q` was implemented on two listings and sent by no screen.** The console
    had no search box at all.

One gap is recorded elsewhere rather than here because it is a product decision,
not a defect: there is no report-detail endpoint behind the moderation queue.

## The pattern worth naming

Ten of these are the same shape: **a column or field written by one side and
read by neither, or read by one side and written by neither.** The lexicon, the
registry, `device_label`, `deprecated_at`, `hidden_at`, `safety_reports.status`,
`has_attestation`, `under_takedown`, `burn_score`, `archived_at`.

**Eleven.** `idempotency_keys.user_id` is the same shape and the worst instance
of it: written on every row since migration 0002, read by nothing, and the thing
it would have been read for is telling one caller's key from another's. The
gates below catch the direction where a column is FILTERED and never written;
this is the reverse, and nothing here looks for it. Worth remembering when the
next one turns up, because a column that is written and never read looks
correct from every side and costs nothing until someone asks what it was for.

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
