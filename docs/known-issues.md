# Known issues

Findings from the console build-out that are real, reproduced, and not yet
fixed. Each says what is wrong, how to see it, and why it matters — so the next
person to pick one up does not have to rediscover it.

This file lives in the repository rather than in someone's notes because the
first version of it lived in a scratch directory and was lost when the container
was reclaimed. Findings are only worth having if they outlive the session that
found them.

Ordered by what I would fix next. Fixed and removed so far: the parental-consent
check on speaking-slot booking, `POST /orders` billing an org the buyer had no
relationship with, paying an order granting nothing, and `view`/`assign` content
grants reaching nothing.

---

## Nothing outstanding

Every finding recorded here has been fixed. Kept as a record of what the
console build-out turned up, and of the pattern below, because the pattern is
the part likely to recur.

Fixed, in the order they were taken:

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

One gap is recorded elsewhere rather than here because it is a product decision,
not a defect: there is no report-detail endpoint behind the moderation queue.

`consents.revoked_at` was on that list and should not have been. It is not a
product decision that a parent cannot withdraw consent for stranger matching;
it is the same defect as the rest, in the one place where the consequence is a
child-safety control that only switched one way. `DELETE /me/consents/{kind}`
now exists.

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
  * **`docker-compose.yml` built the wrong Dockerfile stage.** The four
    application services had `build: {context: ., dockerfile: Dockerfile}` and no
    `target:`. A multi-stage build with no target builds the *last* stage, which
    here is `caddy` — so the api, the worker, the scheduler and the migrate
    one-shot would every one of them have come up as a web server image and died
    on `uvicorn: executable file not found`. The production deployment could not
    start at all.

    Nothing caught it because nothing here builds an image: CI does not (§12 of
    the CI doc argues the eleven minutes are not worth it), `docker compose
    config` validates schema and never looks at stages, and a *missing* key
    reads as nothing at all to a human — the file looked right every time it was
    reviewed. `scripts/check_compose_targets.py` now requires every service that
    builds a multi-stage Dockerfile to name a stage, and requires that stage to
    exist, so renaming one is a build failure rather than a deploy failure.

The common factor with the column defects: **two places have to agree and
nothing is looking at the relationship.** The columns are one instance of it; a
Makefile and a workflow are another; a compose file and a Dockerfile are a
third.
