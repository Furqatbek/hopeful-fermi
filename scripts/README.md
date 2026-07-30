# Verification scripts

**Everything here runs from the `Makefile`, and CI runs nothing else.**

```bash
make install                     # pip install -e ".[dev]"
export TEST_DATABASE_URL="postgresql+psycopg://postgres@localhost/postgres"
export REDIS_URL="redis://localhost:6379/15"
export S3_ENDPOINT="http://localhost:9000"   # any MinIO; CI runs its own
make ci                          # the whole pipeline, ~55 s
```

`make help` lists the targets. `.github/workflows/ci.yml` contains no commands of
its own, so a failing pipeline is reproducible with one command — see
`docs/design/0011-ci.md`.

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
```

Goes beyond schema conformance: catches dangling `$ref`s, undeclared tags and path
parameters, operations missing tags/summary/responses, unreachable schemas, and
OpenAPI 3.0 leftovers such as `nullable: true` that 3.1 accepts silently and then
mistranslates in every client generator.

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
is printed but only enforced at 80%, far below the real 87%, as a tripwire for
the suite collapsing rather than as a goal.

`docs/design/0011-ci.md` §10 has the reasoning and the four defects the first
measurement found, including a `TypeError` that failed a student's submission
whenever a band map did not cover their section's raw score. §11 covers the auth
router, which was 59% and whose unexecuted half contained a complete
authentication bypass.

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

make test-unit      # 377 tests, 1.0 s — no database, no ffmpeg
make test           # everything, ~1m18s
make test-fast      # everything under -n 4, ~33 s
make coverage       # the same, plus the per-path floors, ~46 s
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
