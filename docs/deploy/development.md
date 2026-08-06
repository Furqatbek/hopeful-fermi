# Running it in development

The mode with no secrets, no TLS and no Docker. If you are deploying, you want
[production.md](production.md); if you are onboarding a first prep centre,
[pilot.md](pilot.md).

## What you need

- Python 3.12
- PostgreSQL 16 and Redis 7 (a local install or `docker compose up postgres redis`)
- ffmpeg, for audio ingest. Everything except transcoding works without it.
- Node 22, for the console.

## Getting up

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

export ENVIRONMENT=development
export DATABASE_URL=postgresql+psycopg://postgres@localhost/ielts
export REDIS_URL=redis://localhost:6379/0

alembic upgrade head
uvicorn app.api.main:app --reload
```

`ENVIRONMENT=development` is the only reason this works without secrets:
`app/platform/config.py` refuses to construct `Settings` at all when it is
anything else and `JWT_SECRET` or `TURN_SECRET` is short or still the
placeholder. That is deliberate — those two sign every access token and derive
every media grant — and it means you cannot accidentally run production
configuration by forgetting a variable.

The console:

```bash
cd web
npm ci
npm run codegen      # regenerates the typed client FROM openapi/openapi.yaml
npm run dev
```

`npm run codegen` is not optional after a contract change. The client is
generated, and `make web-codegen-check` fails the build when the committed copy
has drifted — which is the trap it exists to close: a build that silently uses a
stale client.

## Your first account

**A fresh database has no users, and you cannot register through the API.**
Registration goes through `POST /auth/telegram/verify`, which requires a payload
signed with a real bot token — deliberately, because the version that accepted a
bare phone number was a complete authentication bypass. So the first account is
created with psql, the same way the first platform admin is:

```sql
INSERT INTO users (phone, given_name, date_of_birth, locale, status)
VALUES ('+998901234567', 'Aziza', '2000-01-01', 'uz-Latn', 'active');

-- Platform admin, if you want the console's admin screens. `granted_by` is
-- itself, which is what a bootstrap looks like.
INSERT INTO platform_role_grants (user_id, role, granted_by)
SELECT id, 'platform_admin', id FROM users WHERE phone = '+998901234567';
```

Then sign in as that number. With `PILOT_OPEN_SIGNIN=true` the whole loop is
two curls:

```bash
CH=$(curl -s -X POST localhost:8000/api/v1/auth/otp/request \
       -H 'content-type: application/json' -d '{"phone":"+998901234567"}')
XID=$(jq -r .challenge_xid <<<"$CH"); CODE=$(jq -r .pilot_code <<<"$CH")

curl -s -X POST localhost:8000/api/v1/auth/otp/verify \
     -H 'content-type: application/json' \
     -d "{\"challenge_xid\":\"$XID\",\"code\":\"$CODE\"}" | jq -r .access_token
```

`pilot_code` is only in the response because `PILOT_OPEN_SIGNIN` is on. It is
account takeover by design and exists for a pilot with no SMS contract — see
[pilot.md](pilot.md). In development it costs nothing; anywhere else it is the
authentication system switched off.

## Signing in without the pilot flag

There is no SMS provider (see [pilot.md](pilot.md) for why that is deliberate),
so the other path is to read the code out of the database:

```sql
SELECT phone, created_at FROM otp_challenges ORDER BY created_at DESC LIMIT 1;
```

That gives you the challenge but not the code — `otp_challenges` stores only a
hash, on purpose. The plaintext travels through `notifications.params` to the
worker:

```sql
SELECT params FROM notifications
 WHERE template = 'auth.otp' ORDER BY created_at DESC LIMIT 1;
```

`notify.deliver` strips it the moment the row is `sent` or `failed`, so read it
before the worker runs, or do not run the worker. Alternatively set
`PILOT_OPEN_SIGNIN=true` and the code comes back in the response — that is what
it is for, and in development it costs nothing.

## The tests

```bash
make ci-checks    # no services needed: lint, contracts, mypy, the OpenAPI
                  # gates, the console coverage gate, and the unit suite
make ci-tests     # needs PostgreSQL, Redis, MinIO and ffmpeg
```

Two things worth knowing before you write a test here:

**The integration suite shares one database and truncates between tests.** Two
suites in parallel will truncate each other's data mid-test. If you need to run
something else against that server at the same time, serialise it — and note
that a `pg_dump` or a `CREATE DATABASE` running alongside is enough to fail the
timing-sensitive realtime tests.

**Prove a guard by breaking it.** Every rule in this codebase that refuses
something has been verified by deliberately breaking the implementation and
watching the test fail. A test that passes against a broken implementation is
not a test, and this repository has found several. Run a baseline first: `pytest
-k` that matches nothing also exits non-zero, and that looks exactly like a
successful sabotage.

## Where things are

| | |
|---|---|
| `app/api/` | routers, DTOs, dependencies — thin, no business logic |
| `app/modules/` | the domain: authz, content, exam, qtypes, identity, analytics, billing |
| `app/platform/` | database, config, errors, storage, rate limits — no domain knowledge |
| `app/workers/` | the actor pool and the scheduler |
| `web/src/features/` | one directory per console screen |
| `openapi/openapi.yaml` | the contract, and the source of the typed client |

`import-linter` enforces the direction: `app.api` may import `app.modules`,
`app.modules` may import `app.platform`, and nothing goes the other way.
`make contracts` checks it.
