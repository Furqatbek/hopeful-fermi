# Test suite speed

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Continues:** `0009-media.md`
- **Closes:** the `CREATE DATABASE ... TEMPLATE` item, open since `0007`.

---

## 0. Result

```
before        6m 45s
after         1m 18s     serial
after           32s     -n 4
```

755 tests, 8 import contracts kept, OpenAPI unchanged at 149/149.

---

## 1. I was wrong about where the time went

For three documents I carried this item as "the `CREATE DATABASE ... TEMPLATE`
optimisation is overdue", on the theory that the Alembic run dominated. That was
an assumption, and measuring it first would have saved two of those documents.

```
alembic upgrade head            5.4 s   ONCE per session
TRUNCATE (per test)           668   ms   × ~400 tests  =  4m 27s
```

The migration was never the problem. **The per-test `TRUNCATE` was essentially
the entire suite.**

Two further measurements decided the fix:

```
TRUNCATE ... RESTART IDENTITY CASCADE   667.7 ms
TRUNCATE ... CASCADE                    875.4 ms
TRUNCATE + synchronous_commit = off     664.6 ms      ← not fsync
DELETE FROM (all 79 tables)               2.2 ms      ← 300× faster
```

`synchronous_commit = off` changing nothing is the informative one: the cost is
not durability. It is per-RELATION work — `TRUNCATE` takes an ACCESS EXCLUSIVE
lock and rewrites the file for every table **and every index**, and this schema
has 430 relations. The tables are nearly empty after each test, so `DELETE` has
almost nothing to do.

A template database is still worth having, and it is in. It saves the 5.4 s
migration per session — modest alone, and the reason `-n 4` is now viable at all,
because otherwise every worker pays it.

---

## 2. The reset was also incomplete

The old teardown named 32 tables and relied on `CASCADE` to reach the rest. It
reached 64 of 79. **Fifteen tables were never cleaned between tests:**

```
attempt_answer_events   idempotency_keys      notifications
attendance_facts        item_exposure_stats   otp_challenges
audit_log               item_exposures        payment_events
feature_flags           item_stats            payment_reconciliations
prices                  products              user_skill_progress
```

`CASCADE` only reaches tables with a foreign key *into* the truncated set, and
`notifications.user_id` has no FK at all. Rows leaked across the whole session.
Nothing failed, because a leak produces a passing test until the day it produces
a baffling one — and the tests that count notifications happened to create a
fresh user each time.

The list is now derived from `pg_class` at session start, so a migration that
adds a table gets it cleaned automatically, and
`tests/integration/test_fixture_hygiene.py` asserts nothing has slipped out.

### 2.1 Why `session_replication_role = replica`

An ordered `DELETE` cannot work on this schema. Two obstacles:

**Seven foreign-key cycles**, which no topological ordering satisfies:

```
tests ↔ test_versions          attempts ↔ score_runs
passages ↔ passage_versions    questions ↔ question_versions
question_groups ↔ …            band_maps ↔ …        cue_card_sets ↔ …
```

**Three append-only triggers** whose entire purpose is to make their tables
undeletable — `audit_log`, `item_exposures`, and `attempt_answers` after submit.
`TRUNCATE` bypasses row triggers; `DELETE` does not.

`SET LOCAL session_replication_role = replica` disables both FK and user triggers
for the teardown transaction only. It needs superuser, which a local `postgres`
role has and which `scripts/README.md` now states as a requirement.

### 2.2 What was given up

`RESTART IDENTITY`. Sequences now climb across a session. Nothing depended on it —
no test asserts a specific row id, and one that did would be asserting something
the database does not promise.

Re-seeding the registry from 19 JSON files on every test also went (78 ms). The
seeded rows never change, so they are excluded from the wipe; only
test-registered types are removed, matched on `source <> 'builtin'`, because
those carry a `created_by` reference into `users` and would outlive the user who
made them.

---

## 3. Two real bugs the speedup surfaced

### 3.1 A test that failed every night after 22:00 Tashkent

`test_a_deleted_user_is_suppressed_not_retried` queued a non-urgent notification
at the wall clock and immediately asserted delivery. Quiet hours defer anything
non-urgent generated between 22:00 and 08:00 local to the morning, so after
22:00 the notice was never picked up and the status stayed `queued`.

It had nothing to do with the change — the faster suite simply ran at a different
time of day and caught it. Nothing about suppressing a deleted user depends on
the hour, so the time is now stated rather than inherited.

### 3.2 `pytest -n 4` reported "355 passed, 400 skipped" — and exited 0

Four workers reached the template build simultaneously, their `CREATE DATABASE`
calls collided, and the `except: pytest.skip(...)` around it swallowed every
failure. **More than half the suite never ran and the exit code was 0.**

Two fixes, and the second is the important one:

1. The template build is serialized with a Postgres advisory lock. The first
   worker builds; the rest block, then find the fingerprint current and return.
2. **The skip is gone.** Skipping when no database is *configured* is right —
   that is a laptop with nothing installed. Skipping when a database IS
   configured but unreachable is a broken environment reporting success. It now
   errors. Verified both ways: an unset URL skips, an unreachable one produces
   22 errors.

That second failure predates this work. It would have hidden a broken CI
database for as long as nobody read the skip count.

---

## 4. The template's own failure mode

A stale template is worse than no template: the suite would pass against last
week's schema, silently.

The guard is a SHA-256 over every file in `migrations/versions/`, stored as a
comment on the template database. Mismatch means rebuild. Verified by touching a
migration and watching the fingerprint change, then reverting and watching it
change back.

The fingerprint is written **last**, so a template whose migration died halfway
is unlabelled and rebuilt rather than reused.
`tests/integration/test_fixture_hygiene.py` covers the fingerprint function
directly, including that it moves when a file's *contents* change and not only
when one is added.

---

## 5. Where the remaining 78 seconds goes

```
14 s   the ten ffmpeg tests — real encoding, 1.5-2.3 s each
64 s   ~745 tests at ~85 ms — session setup, the `seed` fixture, the 2 ms wipe
```

The ffmpeg time is genuine work and I would not mock it: those tests are the only
thing standing between a loudness regression and a student adjusting their volume
mid-exam.

The remaining ~85 ms per test is mostly the `seed` fixture rebuilding an
organization, two users, a passage, three questions and a published test through
the ORM. Making it session-scoped would help and would be fragile — tests mutate
it freely today. Not worth it at 32 seconds under `-n 4`.

---

## 6. Still open

1. **CI does not run `-n 4`.** There is no CI configuration in the repository at
   all — the import contracts, the OpenAPI validator and the coverage check are
   run by hand. That is the next gap, and it is larger than this one.

2. **`session_replication_role` needs superuser.** Fine locally; a managed
   Postgres that does not grant it would leave the reset unable to clear
   `audit_log` and `item_exposures`. The failure would be loud (the append-only
   trigger raises) rather than silent, which is the right direction, but it is a
   deployment constraint worth knowing before choosing a CI database.

3. **Sequences are never reset**, so a very long session climbs ids indefinitely.
   Harmless at `bigint`.

4. **The 2 ms reset runs even for tests that touched nothing.** Roughly a third of
   the integration suite is read-only. Tracking dirtiness would save perhaps a
   second overall — noted so nobody re-derives it, not proposed.
