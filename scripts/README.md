# Verification scripts

Both are run against a scratch database, not production.

```bash
createdb ielts_verify
export DATABASE_URL="postgresql+psycopg://postgres@localhost/ielts_verify"
alembic upgrade head

# 1. Database-enforced invariants (docs/design/0002-data-model.md section 10).
#    Each ERROR line is an invariant firing correctly.
psql "$DATABASE_URL" -f scripts/verify_invariants.sql

# 2. Acceptance test: add a new question type end to end with zero DDL
#    (docs/design/0002-data-model.md section 5). Depends on the fixtures
#    created by verify_invariants.sql, so run it second.
python3 scripts/acceptance_new_question_type.py
```

`acceptance_new_question_type.py` fails loudly if the schema fingerprint changes,
which is the whole point: adding a question type must never require a migration.

## 3. OpenAPI contract

```bash
pip install openapi-spec-validator pyyaml
python3 scripts/validate_openapi.py
```

Goes beyond schema conformance: catches dangling `$ref`s, undeclared tags and path
parameters, operations missing tags/summary/responses, unreachable schemas, and
OpenAPI 3.0 leftovers such as `nullable: true` that 3.1 accepts silently and then
mistranslates in every client generator.

## 4. Capacity check

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

## 5. Integration tests

```bash
export TEST_DATABASE_URL="postgresql+psycopg://postgres@localhost/postgres"
python3 -m pytest tests/integration -q
```

Each run creates its own scratch database, applies the **real migrations** (not
`create_all`, so the ORM models are proven against the actual schema), and drops
it afterwards. Without `TEST_DATABASE_URL` the suite skips cleanly and the unit
tests still run.
