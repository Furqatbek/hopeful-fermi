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

Two gaps are recorded elsewhere rather than here because they are product
decisions, not defects: `consents.revoked_at` has no withdrawal endpoint, and
there is no report-detail endpoint behind the moderation queue.

## The pattern worth naming

Ten of these are the same shape: **a column or field written by one side and
read by neither, or read by one side and written by neither.** The lexicon, the
registry, `device_label`, `deprecated_at`, `hidden_at`, `safety_reports.status`,
`has_attestation`, `under_takedown`, `burn_score`, `archived_at`.

Every one passed every gate this repository has, because every one is locally
correct: the column exists, the write works, the read works, the check compiles.
Nothing tests the seam between them.

`scripts/check_schema_conformance.py` catches the subset where a *declared*
field is never emitted. It cannot catch a column that no contract mentions. If
one more of these turns up, the gate worth writing is the reverse direction:
every column in a table that some query filters on, and no code path writes.
