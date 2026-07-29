# The workers

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Continues:** `0007-remaining-endpoints.md`
- **Closes:** open item 1 of that document ("the workers are not written"), and
  part of item 4 ("item statistics are read but never written").

---

## 0. What is delivered

Two processes. That is the whole deployment change.

```
dramatiq app.workers.actors         # the actor pool
python -m app.workers.scheduler     # the outbox relay and the periodic ticks
```

```
unit         310 passed          (no database needed — 76 of them new)
integration  380 passed          (real PostgreSQL, real migrations, real HTTP)
──────────────────────────────
total        690 passed in 5m16s

import contracts   8 kept, 0 broken   (three new, all verified to fire)
openapi            PASS  113 paths / 149 operations
coverage           149/149 (100%)
migrations         0001..0019 apply, downgrade to base, and re-apply
```

| Layer | Files | Why it is where it is |
|---|---|---|
| Pure domain | `speaking/matching.py`, `competitions/ranking.py`, `competitions/schedule.py`, `analytics/stats.py` | No database, no clock. 76 tests in 0.08 s. |
| Module services | `speaking/service.py`, `competitions/service.py`, `analytics/projections.py`, `identity/notify.py`, `exam/planner.py` | Load, call the pure core, write. |
| Workers | `workers/{relay,actors,scheduler,sweeper,runtime,broker}.py` | Thin: open a transaction, call a module, done. |

---

## 1. The relay is the whole argument against Kafka

`app/workers/relay.py` is 122 lines, and it is what ADR-0001 §6 promised in place
of a message bus. A domain event is written to `outbox` in the SAME transaction
as the change that caused it, so "a regrade that happened without an event" and
"an event for a regrade that rolled back" are both unrepresentable.

Three decisions in it are load-bearing.

**Send, then mark.** Not the other way round. A crash between the two duplicates
the message; a crash the other way round loses it. Duplicate is recoverable —
every actor is idempotent — and lost is not.
`test_a_send_that_succeeds_but_whose_mark_is_lost_redelivers` pins this, and it
is written so that a change to mark-first makes it FAIL rather than pass: it
asserts the duplicate arrives, not merely that nothing errored.

**`FOR UPDATE SKIP LOCKED`.** Two relays run without coordination and without
double-dispatch, so the deploy story is "restart it" rather than "make sure only
one exists".

**Exhausted rows stay visible.** After eight attempts a row stops being retried
but is neither deleted nor marked dispatched. A dead letter you cannot see is a
dead letter you will not fix, and `outbox_stuck` counts them.

The alarm is one query:

```sql
SELECT now() - min(created_at) FROM outbox WHERE dispatched_at IS NULL;
```

Green under 5 s, page over 60 s sustained (Deliverable 5 §5). It covers regrade,
notifications, analytics projections and webhook retries at once, because they
all enter through the same table. Served at `/metrics/workers`, deliberately
outside the OpenAPI document — it is an operations endpoint, not a contract.

---

## 2. The safety invariant now has a home

"Minors must never be matched 1:1 with adult accounts. Enforce age banding at the
matching layer, not in the client." That was a requirement with nowhere to live
until `app/modules/speaking/matching.py` existed. It now lives there, and the
enforcement is **structural rather than a filter**: candidates are PARTITIONED on
`is_minor` before any pairing happens, so a cross-band pair is not rejected — the
two candidates are never in the same list. A final assertion re-checks each pair
and raises rather than returning it.

Three things about this are worth defending explicitly.

**`is_minor` is read from `users.adult_at` at match time.** Not from the slot's
`age_band`, not from the booking, not from anything a client sent. A slot a
teacher mislabelled `adult` still cannot put a fourteen-year-old in an adult
pair, and `test_a_minor_is_never_paired_with_an_adult_even_in_an_adult_slot`
proves it against the database.

**`mixed_supervised` does not weaken the rule — an opinionated reading.** That
band exists so a teacher-run cohort session may contain both minors and adults in
one ROOM. It does not authorize a minor–adult PAIR, and the matcher partitions on
age regardless of what the slot says. A supervising adult with no adult peer goes
unmatched, which is correct: they are supervising, not practising. The looser
reading — "mixed means mixed" — would satisfy the schema and violate the
requirement, and this is the kind of place where the schema being satisfiable two
ways means someone has to choose.

**The matcher is deterministic.** Same input, same output, every time. A matcher
that shuffles cannot be reproduced when a student reports an unsuitable partner,
and a safety investigation that cannot reproduce the decision is not much of an
investigation.

The suite is adversarial rather than confirmatory: 28 tests asking "what would I
have to do to get a cross-band pair out of this function", including identical
bands (the strongest pull the algorithm has), a lone minor among four adults (the
case a greedy implementation gets wrong), and five ratios from 1:1 to 9:1.

---

## 3. Regrade, end to end

`app/modules/exam/planner.py` turns a staged job into an impact report and then
into scores. Every promise Deliverable 1 made about fixing a bad answer key is
now checked against a database:

| Promise | Test |
|---|---|
| The dry run touches nothing | `test_the_dry_run_reports_the_impact_and_writes_no_scores` |
| History survives | `test_applying_supersedes_rather_than_edits` |
| Unchanged attempts get no new run | `test_an_unaffected_attempt_gets_no_new_run` |
| Only band changes are notified | `test_only_students_whose_band_moved_are_notified` |
| A redelivery notifies once | `test_applying_twice_does_not_notify_twice` |
| Previews are never regraded | `test_previews_are_never_regraded` |
| A contest is never swept in | `test_a_competition_attempt_is_not_swept_into_a_bulk_regrade` |

**Planning and applying both recompute.** The dry run's output is a report a
human reads, possibly hours before deciding; keys can move in between, and
applying a stale plan would mark students against a key nobody approved. At
1.24 ms per attempt, a full recompute of every attempt this platform will hold in
year one is about a minute of CPU — correctness is simply cheaper than caching.

**Scoring inputs are loaded once per TEST VERSION.** Forty students sitting the
same mock share one item set, one key set and one band map. Resolving them per
attempt is the difference between five queries and two hundred.

**The planner calls `ExamSession`'s own resolver** rather than a parallel query.
Two implementations of "which items are on this test" would eventually diverge,
and the divergence would look like a scoring bug.

---

## 4. Cost decisions, because on this budget they are engineering decisions

**Telegram over SMS, always.** SMS is the only user-linear line on the infra
bill. `identity/notify.py` routes to Telegram whenever the account has a link,
falls back to in-app for everything, and reserves SMS for exactly one case: a
login code for an account with no Telegram. At 1,500 users a careless
notification design is the difference between $0 and $60 a month.
`sms_cost_minor_this_month` is in the health snapshot for that reason.

**Quiet hours in the user's own timezone.** A push at 02:00 does not get read, it
gets the app muted. Non-urgent notices generated between 22:00 and 08:00 local
are deferred to 08:00. `auth.otp`, `safety.action_taken` and
`competition.starting_soon` are exempt, and that list should stay short —
everything on it is something the user is waiting for right now.

**One loop process, not a cron library.** Dramatiq has no scheduler. The options
were a third dependency, a system crontab kept in sync with the code by hand, or
about sixty lines in `scheduler.py`. Sixty lines beside what they schedule, in
the same repository, deployed by the same command, is the right answer at this
size — and the deploy story stays "two processes".

---

## 5. Six defects the tests caught

### 5.1 Actors bound to the wrong Redis, silently

`@dramatiq.actor` binds `dramatiq.get_broker()` **at decoration time**, and
`get_broker()` helpfully invents a `RedisBroker` on `localhost:6379` when none is
installed. Importing `app.workers.actors` before configuring the broker therefore
bound every actor to a broker pointing at the wrong host — and the only symptom
was messages that went nowhere. No exception, no log line.

Found by running the real processes rather than by reading the code:
`redis-cli keys 'dramatiq:*'` was empty after a successful-looking dispatch.
`actors.py` now calls `broker.current()` above its first decorator, with the
reason written next to it.

### 5.2 `uuid = character varying`, again — and it will keep happening

Two actors bound `str` xids into `WHERE xid = :x`. This is the same defect that
hit ten API endpoints in the previous phase, and here it was **unavoidable by
construction**: Dramatiq serialises arguments to JSON, so an xid always arrives
as a string and there is no FastAPI in the path to parse it.

Fixed everywhere by casting in the SQL — `WHERE xid = CAST(:x AS uuid)` — which
works whichever type the caller binds. That is now the rule for raw SQL against a
`uuid` column, applied to all 22 sites.

`tests/integration/test_actors.py` exists specifically for this class: every
actor is invoked with the exact argument types a JSON payload produces. Reverting
the cast fails four of its tests.

### 5.3 Every item statistic was double-counted for self-serve attempts

```python
for org in (None, row["org_id"]):        # collapses to (None, None)
```

Intended to write a global row and a per-centre row. For an attempt with no org
context both entries are `None`, so every response was counted twice — surfacing
as "6 students wrote 'bike'" when three did. That number is the most useful
output in the analytics path, the one that finds a missing key alternative, and
it was quietly wrong. A `set` fixes it.

### 5.4 `attendance_facts` rejected every student who had not started

`a.status IN ('submitted','scored')` over a `LEFT JOIN` yields NULL, not false,
when the student has no attempt — and the column is `NOT NULL`. The projection
failed outright on exactly the students a teacher most wants to see.

### 5.5 `item_stats` had no key to upsert on

The table is designed to hold one row per (item, org, window) and had no unique
constraint saying so, so the projection would have inserted a fresh set every run
and the flagged-items dashboard would have shown each item several times with
different numbers. Migration 0019 adds it, with `coalesce(org_id, 0)` — NULLs are
distinct in a unique index, so a plain three-column index would still have let the
global row duplicate.

### 5.6 An import contract that reported KEPT against a real violation

`layers = ["app.api : app.workers", ...]` — the sibling syntax — reports KEPT on
import-linter 2.13 with an actual `app.workers → app.api` import present. I found
it by injecting the violation to check the contract worked, which is the only
reason it was caught: a contract that passes while the rule is broken is worse
than no contract, because it is trusted.

Replaced with two `forbidden` contracts, and both were verified to FAIL against
an injected violation before being kept. The same check should be applied to any
new contract.

That defect also forced a real fix rather than a cosmetic one: `actors.py` had
been reaching into `app.api.deps` to borrow the API's cached registry. The
registry singletons now live in `app.modules.qtypes.registry`, where both
composition roots can reach them, and the health queries moved to
`app.platform.health` for the same reason.

---

## 6. Still open

1. **Media is still not stored.** Unchanged from the previous document.
   `_sign_grant` hashes rather than HMACs, `presigned_urls` is empty, and
   `GET /media/{xid}/content` returns an empty body. **No audio transcode worker
   exists** — the ADR specifies an ffmpeg subprocess, and writing it before there
   is object storage to read from and write to would be writing it twice. This is
   the largest remaining gap and it blocks the listening product.

2. **The realtime gateway is still a stub.** `POST /realtime/ticket` mints a
   ticket; no WebSocket server consumes it, and the ticket is not written to
   Redis. Exam timing does not depend on it.

3. **The notification transport is a logging stub.** The queue, the channel
   selection, the quiet hours, the retry ladder and the cost accounting are all
   real and tested; `Transport.send` writes a log line instead of calling
   Telegram or an SMS gateway. Swapping it in is one class.

4. **`option_distribution` is collected but `discrimination` reaches the API as
   `null`.** `refresh_item_stats` computes both correctly; the
   `/test-versions/{xid}/item-analysis` endpoint still computes p-values live
   from `item_scores` rather than reading `item_stats`, so it does not see them.
   Pointing that endpoint at the projection is a small change and was left out of
   this phase to keep it to workers.

5. **The live-queue matcher runs on a 15-second tick.** Correct, and it means a
   student who joins the queue waits up to 15 seconds after a compatible partner
   appears. At current volumes the wait is dominated by there being nobody else
   in the pool; when that stops being true, the matcher wants a wake-up on join
   rather than a poll.

6. **No `docker-compose.yml` in the repository.** The ADR describes the topology
   and the two commands are documented above, but the file itself does not exist.
   It should land with the first deployment, not before.

7. **The integration suite is 5m16s.** The `CREATE DATABASE ... TEMPLATE`
   optimisation named in the previous document is now overdue.
