# Verification scripts

**Everything here runs from the `Makefile`.**

```bash
make install                     # requirements-dev.txt (hash-checked), then pip install -e . --no-deps
export TEST_DATABASE_URL="postgresql+psycopg://postgres@localhost/postgres"
export REDIS_URL="redis://localhost:6379/15"
export S3_ENDPOINT="http://localhost:9000"   # any MinIO; CI runs its own
make ci                          # the whole pipeline
```

`make help` lists the targets.

Nearly every CI step is a `make` target, so a failing pipeline is reproducible
with one command. **Not all of them are** — `ci.yml` has three raw `run:` steps
(an apt install, two inline scripts), and gates have drifted out of the
workflow while staying in `make ci`. `make ci-parity`
(`scripts/check_ci_parity.py`) fails when a gate in `make ci` has no workflow
step, and it exists because that happened rather than as a precaution. It
counts `make <target>` in steps only: the workflow explains its steps in
comments that name the same targets, and until `tests/platform/test_ci_parity.py`
pinned it, a deleted `run:` line stayed green through its own explanation. See
`docs/design/0011-ci.md`.

Two more gates of the same shape — two places that must agree, and a check on
the relationship:

- `make lock-check` — `requirements.txt` and `requirements-dev.txt` are
  `pyproject.toml`'s `>=` floors resolved and hash-pinned by `make lock`
  (`uv pip compile`). The check regenerates beside the committed file and
  diffs; uv keeps the existing pins, so only a change to the floors moves it.
  Before the lock, the Dockerfile, CI and a laptop each resolved the floors on
  their own day and no file said what production actually ran.
- `make client-parity` (`scripts/check_client_parity.py`) — the hand-written
  half of the API client exists twice, in `web/src/api/` and
  `student/src/api/`. `client.ts` carries the refresh-once 401 policy and the
  `ANONYMOUS` list and must be byte-identical; `session.ts` may differ only in
  its localStorage key and the comment justifying it. The generated
  `schema.d.ts` is not asserted here — the two codegen-check targets already
  prove each copy against the contract.

One exception, and it cost a red build: **CI's lint job installs nothing** — no
ffmpeg, no Postgres, no MinIO — and a developer machine has all three, so
`make test-unit` locally could not reproduce it. `tests/platform/conftest.py`
empties `PATH` for that tier, which makes the runner's environment the local one
and turns "these tests need no services" from a convention into a mechanism.

One thing to know before running anything else: **under `CI=true` a skipped test
is a failure.** Skipping without a database or without ffmpeg is right on a
laptop and wrong on a runner whose job is to provide them. `ALLOW_SKIPS=1`
overrides it.

## 1. Database-enforced invariants, and the zero-DDL acceptance test

```bash
make invariants                  # scripts/check_invariants.py
```

Creates its own scratch database, migrates it, runs `verify_invariants.sql`
(`docs/design/0002-data-model.md` §10) and then `acceptance_new_question_type.py`
against the same database, and drops it.

The SQL runs with `ON_ERROR_STOP off` and **a correct run is full of errors** —
each one is an invariant refusing a write. That is why its exit code means
nothing on its own and why the wrapper exists: every expected refusal must fire,
every reported value must match, and nothing else may error.

To read the output by hand instead:

```bash
createdb ielts_verify
export DATABASE_URL="postgresql+psycopg://postgres@localhost/ielts_verify"
alembic upgrade head
psql "$DATABASE_URL" -f scripts/verify_invariants.sql
python3 scripts/acceptance_new_question_type.py    # needs the fixtures above
```

## 2. Migrations

```bash
make migrations                  # scripts/check_migrations.py
```

`upgrade head → downgrade base → upgrade head` on a scratch database, plus an
assertion that the history has exactly one head. The middle step is the one that
finds things: `downgrade` rots quietly, and the day it is needed is the day a
deploy is already going badly.

## 3. OpenAPI contract

```bash
make spec                        # validate_openapi.py + check_api_coverage.py
                                 #   + check_schema_conformance.py
```

Three layers, each catching what the one before cannot.

`validate_openapi.py` — the document itself: dangling `$ref`s, undeclared tags and
path parameters, operations missing tags/summary/responses, unreachable schemas,
and OpenAPI 3.0 leftovers such as `nullable: true` that 3.1 accepts silently and
then mistranslates in every client generator.

`check_api_coverage.py` — every documented **operation** is served, and every
served route is documented.

`check_schema_conformance.py` — every documented **field** is implemented. A
response field pinned to a literal `None` (or an empty `{}` / `[]`); a request
field nothing reads; a `required` response field emitted nowhere; a contract
`enum` the request model accepts any string for; a declared query parameter or
body property the served operation does not bind (found by diffing the contract
against the app's own generated document). Four defects of exactly this shape
shipped before it existed, and it found seven more on its first run; the two
newer checks found twelve unenforced enums and four unbound `org_xid` bodies on
theirs. Findings are fixed, not silenced — `ALLOWED` carries the deliberate cases
and every entry states its reason, and an entry that no longer suppresses
anything fails the run. `docs/design/0011-ci.md` §21 has the four rounds of
false positives it took to make the output worth reading, the hole that only
sabotage found, and what it still cannot see.

Three `ALLOWED` entries described open items rather than deliberate omissions, and
closing them found more than they named. `checksum_sha256` was a value a client
could declare and nothing compared; beside it, `media_uploads` recorded
`expected_bytes` and `received_bytes` in the same row and never compared those
either, so an upload that dropped halfway became a `ready` listening section at
whatever length happened to arrive (§24). `discrimination` and `mean_time_ms` were
listed as unbuilt analysis — but `analytics/stats.py` had computed both all along,
and the endpoint was a second, worse implementation that pinned them to `None`
(§25).

That is worth knowing about this list: **an entry saying "not built yet" is a
claim, and claims go stale.** Both of these had stopped being true before anyone
went back to check.

## 4. Object storage

```bash
export S3_ENDPOINT=http://localhost:9000 S3_ACCESS_KEY=minioadmin S3_SECRET_KEY=minioadmin
python3 -m pytest tests/integration/test_s3_storage.py -q
```

`S3Storage` against any S3-compatible endpoint. Most of the file is
`tests/storage_contract.py` — the same assertions the local backend satisfies in
`tests/platform/test_storage.py`, which is how two divergences got found. The
rest exercises presigned URLs over real HTTP, the one path that carries the
promise that a teacher's 40 MB upload never transits the app server.

CI runs `minio/minio`. Locally, either point it at any MinIO you have or skip it
— but note that CI turns that skip into a failure.

## 5. Workers

```bash
export REDIS_URL="redis://localhost:6379/15"
make smoke                       # scripts/smoke_workers.py
```

Spawns a real `dramatiq` worker process, drains an outbox row through the real
dispatch table, enqueues a job and waits for the row to change in PostgreSQL.

The only check here that starts a second OS process, and the only one that can
see an actor bound to the wrong broker — a failure whose sole symptom is a queue
that stays empty. `docs/design/0011-ci.md` §7 explains why the obvious version of
this test passes on a broken build.

## 6. Coverage

```bash
make coverage                    # the suite + scripts/check_coverage.py
python3 scripts/check_coverage.py --report   # print the table, gate nothing
```

**There is no repository-wide coverage target, on purpose.** A single percentage
rewards testing whatever is cheapest and goes up when you delete a hard-to-test
module. What is gated is a floor per path, each with its justification written
beside it, on the code where an unexecuted line is a security or correctness
risk — `authz`, `grants`, `scoring` at 100%, the rest lower. The overall figure
is printed but only enforced at 80%, far below the real 96%, as a tripwire for
the suite collapsing rather than as a goal.

`docs/design/0011-ci.md` §10 has the reasoning and the four defects the first
measurement found, including a `TypeError` that failed a student's submission
whenever a band map did not cover their section's raw score. §§11-20 cover the
eleven routers measured since, each of which was under 85% and each of which had at
least one defect in the gap: a complete authentication bypass (`auth.py`), an
unauthenticated payment callback that marked any order paid (`platform_ops.py`),
a listening transcript readable by any student at the centre (`assets.py`), a
roster returning every member's phone number and minor flag (`identity.py`), a
safety-evidence upload that recorded the audio without storing it (`speaking.py`),
a capacity-capped contest that returned 500 on every entry (`competitions.py`),
a band map that could not be attached to a test version at all — which, since the
publish gate requires one, meant nothing could be published
(`tests_authoring.py`), a teacher refused permission to assign work to their own
centre's students (`teaching.py`), `assignment_xid` accepted by `POST /attempts`
and never read, so there was no path at all from "a teacher sets a mock" to "a
student sits it" (`exam.py`), and a bulk import that captured no copyright
attestation and had no authorization on it (`authoring.py`).

§22 does the same for `app/modules/competitions/`, where the leaderboard kept a
**disqualified** entry ranked, one malformed row took the whole board write down
with it, and an unknown duration won every tiebreak.

§23 covers `app/modules/content/review.py`, which is new: `content_reviews` had
existed since migration 0008 and **publish read none of it**, so a version a
reviewer had explicitly rejected published unchanged and the submitter could
approve their own request. On the way in, `POST /test-versions/{xid}/validate`
turned out to have no authorization at all — and it returns the publish gate's
findings, which quote the accepted answer verbatim, so a student could read the
key to the paper they were about to sit.

§24 and §25 close the last two "not built yet" entries in the conformance check's
`ALLOWED` list — the media integrity checks, and item analysis, where
`analytics/stats.py` had computed discrimination and mean time all along while the
endpoint pinned both to `None` and flagged items on a single response.

§26 closes the last of them: a centre with a **ten-seat licence** could assign to
four hundred students, because the entitlement check ran against the teacher and
stopped. `entitlements.check` has the seat rule and the assigned path was the one
route that never asked it about a student.

§§27-28 do `app/modules/speaking/` — "enforce age banding at the matching layer,
not in the client", and the one part of the product whose failure is a
child-safety incident rather than a bug. **That invariant has two backstops, in
two files, and neither had ever run.** `matching.py`'s `raise UnsafePair` was
covered by a test that re-implemented the check four lines below and asserted
against the copy; `service.py`'s cross-band refusal on the INSERT carried
`# pragma: no cover`.

The second is the one to take away from this file: **a pragma does not say a line
is safe, it says this gate must not look at it.** Nine of them exist in `app/` and
they are worth re-reading on that basis — the gate cannot tell "unreachable" from
"unreached".

§29 closes what §28 reported and did not fix: a slot's `band_min`/`band_max`
filtered nothing, so a "Band 6–7" session was offered to a band-4 student who
booked it, took a seat, attended, and went unmatched. **Coverage could never have
found that one** — every line involved was executed; they simply led nowhere. The
gate that catches this class is `check_schema_conformance.py`, and only for fields
the code never *reads*; a field read into a DTO and used for nothing is invisible
to both.

§30 closes the limitation §29 left behind: the band being compared was
`users.target_band`, an aspiration that clusters at 7.0. It now comes from what a
student has actually scored. Neither gate could have found that one either — the
field was read, used, and meant the wrong thing.

§31 is the last path to come under a floor it meets, and it corrects a prediction
written on this page: `app/platform/` was supposed to be where a sweep would not
pay — config defaults and storage error branches. Its gap held `unit_of_work`,
**dead**, with byte-for-byte copies in `api/deps.py` and `workers/runtime.py`
doing the work. The transaction boundary every other guarantee in this system
sits on was three implementations that happened to agree, and none of the three
had ever been executed by a test. The prediction was about what kind of CODE was
uncovered; the finding was about what was not wired up.

The pattern across all eleven routers is worth stating on its own: **where the
OpenAPI document and the implementation disagreed, the document was right every
time** — with two exceptions, a required attestation the document had listed as
optional (§20.1) and the `flag_reasons` enum, which listed two reasons nothing
emits and omitted two the implementation has always emitted (§25.1). Two more
promises had been true in the contract and nowhere else: "on success ... an audit
record is written", on publish (§23.3), and the 402 on `POST /assignments` for a
centre with "no seat or entitlement covering **these students**" (§26).

**A new deployment needs three secrets set, and all three fail closed when
empty** rather than degrading to accepting anything:

| variable | what refuses without it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram sign-in — it used to trust the request body |
| `PAYME_MERCHANT_KEY` | the Payme callback — it used to have no auth at all |
| `CLICK_SECRET_KEY` | the Click callback signature |

The payment secrets previously defaulted to `dev-only-change-me`, which is
published in this repository, so a deployment that had not overridden them
accepted credentials anyone could compute.

## 7. Capacity check

The one thing here that is **not** a CI gate. It measures a running production
database, so there is nothing for it to say about a scratch one.

```bash
psql "$DATABASE_URL" -f scripts/capacity_check.sql
```

One query per scaling trigger in `docs/design/0005-scaling-triggers.md`, each
printing its own verdict. Run it weekly — a cron that emails the output is enough
at MVP. Section 8 needs `pg_stat_statements`; everything else is core Postgres.

The two that degrade silently and so matter most: **section 4** (HOT update ratio
on `attempt_answers` — bloat shows up as gradually slower autosaves, never an
error) and **section 6** (outbox lag — the single best worker-health signal, and
it covers regrade, notifications and analytics projections at once).

## 8. Running the test suite

```bash
export TEST_DATABASE_URL="postgresql+psycopg://postgres@localhost/postgres"

make test-unit      # 383 tests, 1.0 s — no database, no ffmpeg
make test           # everything, ~2m10s
make test-fast      # 1513 tests under -n 4, ~55 s
make coverage       # the same, plus the per-path floors, ~94 s
```

Each session creates its own scratch database from a template that is migrated
once and reused, and rebuilt automatically whenever any file under
`migrations/versions/` changes. Under `-n` the template build is serialized with
a Postgres advisory lock, so the workers do not race. Per-test reset is a
catalogue-derived `DELETE` under `session_replication_role = replica`, ~2 ms.
`docs/design/0010-test-suite-speed.md` has the measurements.

**Two things the suite needs from the database role**, both satisfied by a
default local `postgres` superuser:

- `CREATE DATABASE` (the scratch database and the template);
- permission to `SET session_replication_role`, which is superuser-only. Without
  it the per-test reset cannot clear `audit_log` or `item_exposures`, whose
  append-only triggers exist precisely to prevent that.

## 9. Media and audio ingest

The transcode worker shells out to `ffmpeg`/`ffprobe` (ADR-0001 §5.5 — no Python
audio library). Without them the ingest tests skip on a laptop and **fail under
`CI=true`**, because a runner missing ffmpeg is a broken runner, not a reason to
test less. A worker that starts without them fails loudly rather than silently
marking uploads `failed`.

```bash
apt-get install -y ffmpeg          # or brew install ffmpeg

# Storage backend. "file" needs nothing and is the default; "s3" points at any
# S3-compatible endpoint — MinIO locally, Hetzner or a Tashkent IDC in production.
export STORAGE_BACKEND=file
export STORAGE_ROOT=./var/media
```

`docs/design/0009-media.md` §2 explains the two-pass loudness normalisation and
why the obvious single-pass test does not detect a regression.
