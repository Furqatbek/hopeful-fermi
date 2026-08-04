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

## 1. `POST /band-maps` validates nothing

No check that the mapping covers the full raw-score range, that it is
monotonic, or that bands are in range. A map created by an account with no org
becomes a platform default every centre sees.

**Why it matters:** a band map turns a raw score into the band a student is
told they got. A gap in the range produces a wrong band for a whole cohort, and
`score_runs` records which map version produced it — so the damage is durable
and the evidence is right there.

The console guards its own form; a direct API call is unguarded.

## 2. No archive endpoint for most content

`archived_at` is read by four listings and written only for `tests` and
`test_versions`. Questions, passages, audio tracks, question groups and
cue-card sets have the column and no endpoint.

**Consequence:** the exposure screen can tell an author an item is burned and
there is no action behind "retire it".

**More urgent since `view` grants started working.** A centre can now share
material with a partner and the partner can genuinely open it — but there is no
way to withdraw a burned item from circulation, because archiving does not
exist. Revoking the grant removes one partner's access; retiring the item
removes it from everyone's, and only one of those is possible today.

## 3. `Question.burn_score` is declared and hardcoded `None`

**Where:** `app/api/routers/assets.py::question_dto`.

Migration 0009 builds `item_exposure_stats_burn_idx`, commented "The author's
'most burned items' view" — an index built for a listing nobody wrote.

**Consequence:** there is no cross-item exposure listing, so the console's
Exposure screen fetches a page of the bank and issues one request per row.

## 4. `GET /questions` declares `q` and never applies it

`list_questions` accepts the parameter and filters only on `type_key` and
`skill`. No search box is offered on the screens that would use it, because a
box that silently returns everything is worse than none.

## 5. `GET /content-grants` cannot page

Mine. It reads `LIMIT max(limit*4, 200)` raw rows, filters by policy in Python,
then truncates — so past ~200 live grants platform-wide, a centre's own grant
can fall off the list, and `next_cursor` is always null.

## 6. Smaller, all verified

- **`GET /admin/reports` declares `status` and the handler takes
  `status_filter`.** Sending the contract's name is silently ignored. Same
  defect on `GET /regrades`.
- **No listing for `moderation_actions`,** though migration 0014 says its index
  is "shown on every moderation screen". A moderator cannot see what was already
  done about a report.
- **`safety.report_filed` and `safety.action_taken` have no producer.** The
  moderation screen polls every 30 s; the socket only reports liveness.
- **`REALTIME_URL` in `docker-compose.yml` advertises `/api/v1/realtime`;** the
  gateway is at `/realtime`. The client ignores the advertised value and derives
  same-origin, so nothing is broken — but the value is wrong.
- **`web/vite.config.ts` does not proxy `/realtime`,** so the socket does not
  work under `npm run dev`. One line.
- **The published snapshot's `sections[].audio.media_xid` is the audio TRACK's
  xid,** not a media asset's (`content/repo.py:172`). Using it against
  `GET /media/{xid}/content` answers 403 `grant_wrong_media`. Nothing depends on
  it — the player reads `media_xid` off the grant response — but the field lies.
- **A cancelled upload leaves an orphan `audio_tracks` row** stuck in
  `processing` forever. There is no `DELETE /audio-tracks/{xid}`.
- **`/auth/session` returns `memberships[].org_id`,** an internal integer
  primary key, on the public surface.
- **`consents.doc_hash` is stored and never returned.** The part that makes a
  consent evidence rather than a boolean is not readable through the API.
- **`consents.revoked_at` is returned and written by nothing.** No withdrawal
  endpoint exists.
- **`auth_sessions.device_label` is read by `list_devices` and written by no
  code path.** `Device.platform` and `Device.current` are hardcoded, and
  `current` is uncomputable — the access token carries only `sub`, so no handler
  can tell which session a request came from.
- **ESLint has no config and is not installed.** `npm run lint` is broken
  repo-wide. Strict `tsc` with `noUncheckedIndexedAccess` and
  `exactOptionalPropertyTypes` is the only static gate on the console.

---

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
