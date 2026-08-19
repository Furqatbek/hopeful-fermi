# Deploying to production

One VPS, one `docker-compose.yml`, eight containers. Sized for a Hetzner
CPX31-class box — 4 vCPU, 8 GB, 160 GB NVMe — which with a domain is roughly
$18–32/month.

For a first prep centre read [pilot.md](pilot.md) as well: it turns one thing
off that this document assumes is on.

## Before the first `up`

1. **A box with full-disk encryption.** This disk holds minors' dates of birth
   (ADR-0001 §9.2). Not optional, and it cannot be added afterwards without a
   rebuild.
2. **DNS already pointing at it.** Caddy gets its certificate through an ACME
   HTTP-01 challenge — an inbound request on port 80 for a name the CA resolves
   itself — so an unpropagated record is a certificate that never issues.
3. **Ports 80 and 443 open, and nothing else.** Do not open 5432 or 6379.
   `docker-compose.yml` publishes neither, and adding a `ports:` entry would
   expose the database to the internet regardless of your firewall: Docker's
   port-publishing rules sit ahead of the ones `ufw` writes.
4. **Secrets generated**, below.

```bash
git clone <this repo> && cd <this repo>
cp .env.example .env

python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # JWT_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # TURN_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # POSTGRES_PASSWORD

$EDITOR .env          # those three, plus DOMAIN and ACME_EMAIL

docker compose up -d --build
docker compose logs -f
```

That is the whole deployment. The build compiles the console inside the image —
regenerating the typed client from `openapi/openapi.yaml` as it goes — runs
`alembic upgrade head` in a one-shot container before anything reads the schema,
and starts everything else behind Caddy. The first build is about 1 GB, roughly
half of it ffmpeg's codec libraries; later deploys move only the layers above.

### It is supposed to fail if you skip the secrets

`app/platform/config.py` refuses to construct `Settings` at all when
`ENVIRONMENT` is not `development` and `JWT_SECRET` or `TURN_SECRET` is under 32
characters or still holds the value printed in that file. It raises at import,
before the FastAPI application exists, so the symptom is a container that will
not start and says why. Compose adds an outer gate: `${VAR:?...}` means it
refuses rather than substituting an empty string.

That is the intended behaviour. `jwt_secret` signs every access token and
derives the media-grant and competition-payload keys, so a deployment that
forgot it lets anyone who has read this repository mint a token for any user,
including a platform admin.

## Your first account

There is no sign-up, and no endpoint grants `platform_admin` — an API that
could mint one is a far larger blast radius than a step performed once, out
of band. Fill in the `BOOTSTRAP_*` block in `.env` (`.env.example` has the
full set and what each one does) before the first `up`, and the `migrate`
service creates the account right after running the schema migration:

```bash
BOOTSTRAP_ADMIN_PHONE=+998901234567
BOOTSTRAP_ADMIN_NAME=Your Name
BOOTSTRAP_ADMIN_DOB=1990-01-01

# optional — set both, or leave both unset
BOOTSTRAP_ORG_NAME=Your Centre
BOOTSTRAP_ORG_SLUG=your-centre
```

Leaving the org fields unset gives a platform-admin-only account with no
centre attached — the shape to use if you plan to create and hand off centres
to other people rather than run one yourself. Setting them makes the same
account the new centre's `centre_admin` directly, no invitation to yourself
required.

`scripts/bootstrap.py` is safe to leave configured indefinitely: it is a
no-op whenever `BOOTSTRAP_ADMIN_PHONE` is unset, and a no-op whenever an
unrevoked platform admin already exists, checked before it touches anything
else — so a stale value here after the first successful boot does not
re-grant or duplicate on a later redeploy. If you ever need a **second**
platform admin, that one still goes through `psql` by hand
(`docs/deploy/development.md`, "Your first account") — this script only ever
creates the first.

## What runs

| | |
|---|---|
| `caddy` | TLS and reverse proxy. The only container publishing a port. |
| `api` | gunicorn, 4 uvicorn workers. The arithmetic is commented in the compose file. |
| `worker` | `dramatiq app.workers.actors` — all five queues |
| `scheduler` | the relay and the periodic ticks. Exactly one process, by design. |
| `backup` | nightly dump, media archive, and a restore verification |
| `migrate` | `alembic upgrade head`, once, before anything else starts |
| `postgres` | PostgreSQL 16 |
| `redis` | broker, cache, leaderboards |

Every image is pinned by tag **and** digest. The bump procedure is commented in
`Dockerfile`.

## TLS

Caddy obtains and renews the certificate itself and reloads without dropping a
connection. There is no certbot, no renewal cron, no deploy-hook and no
`fullchain.pem` path to get wrong — that is why it was chosen over nginx for a
solo maintainer (ADR-0001 §2.4), and adding those parts back would give the same
result with five more things to keep working.

The `caddydata` volume holds the issued certificates and the ACME account key
and is **not** optional: without it every restart re-issues, and Let's Encrypt
allows five duplicate certificates per week — so a few restarts on a bad
afternoon leave the site without TLS for days.

## What Caddy routes where

`Caddyfile` is a routing table and the list of backend prefixes is the part to
get right. It is not just `/api`:

| prefix | goes to | why it matters |
|---|---|---|
| `/api/*` | the API | the contract, all 180 operations |
| `/realtime`, `/realtime/*` | the API | the WebSocket gateway; deliberately outside `/api/v1`, because `check_api_coverage.py` compares that prefix against the OpenAPI document and a socket is not an HTTP operation |
| `/internal/*` | the API | **how listening audio reaches a student** under `STORAGE_BACKEND=file` |
| `/healthz`, `/metrics/*` | the API | operations, deliberately out of the contract |
| everything else | `/srv/web` | the console, with an SPA fallback to `index.html` |

Drop `/internal/*` from that matcher and a media request returns the SPA shell —
HTTP 200 with HTML where an m4a should be. Every listening exam breaks and the
console looks perfectly healthy. Verified by doing exactly that against a real
Caddy with a stub upstream, which is also how the cache headers were checked:
hashed assets immutable for a year, the shell `no-store` on every path that
serves it.

The console dev server proxies `/realtime` as well as `/api`, with `ws: true`,
so the socket works under `npm run dev`. It did not always: the proxy covered
`/api` alone and the dev server answered the handshake itself.

## Backups

The `backup` container runs on start and then daily at `BACKUP_AT_HOUR_UTC`
(default 03:00 UTC, which is 08:00 in Tashkent — after the night, before the
first class). Each run:

1. `pg_dump -Fc` the database,
2. **then** `tar` the media,
3. write checksums and a manifest,
4. prune to 7 daily and 4 weekly,
5. restore the result into a scratch database and check it.

**The order in steps 1 and 2 is load-bearing.** The two stores reference each
other, so whichever is captured second must be the superset. Dump-then-media
means the archive holds everything the dump names plus anything uploaded in
between — those extras are orphans, which cost disk and break nothing. The other
order lets the dump reference a file the archive does not have, which is a
listening section a student opens to a 404 in a timed exam.

Step 5 is the one that matters. An untested dump is a hypothesis. It restores
into a throwaway database beside the live one — never over it — and asserts the
checksums match, the restore succeeds, the restored row counts are consistent
with the live ones, no attempt references a missing test version, and the
recently-uploaded media the database names is actually in the archive. That last
check is the only thing in this system that looks across both stores.

Watch for this line, and alert on its absence:

```
[verify] OK: 20260803T030000Z restores, and its media matches its database
```

### These are not yet backups

`backups` is a Docker volume on the same disk as `pgdata` and `media`. It
survives `DROP TABLE`, a bad migration and a botched deploy. It does not survive
a failed NVMe or a lost box.

Copy it to a second machine **in-country** — §9.1: personal data of Uzbek
citizens, including minors, may be required to stay here, so a default of "some
bucket in Frankfurt" would be a legal problem shipped as a convenience. Put the
job on the *receiving* side, so a compromised app box cannot delete its own
history:

```bash
# on the backup host, hourly
rsync -az --delete \
  ielts-prod:/var/lib/docker/volumes/ielts_backups/_data/ \
  /srv/ielts-backups/
```

## Restoring

```bash
docker compose stop api worker scheduler

docker compose run --rm backup /scripts/restore.sh              # lists what exists
RESTORE_CONFIRM=yes-destroy-the-current-data \
  docker compose run --rm -e RESTORE_CONFIRM \
  -v ielts_media:/var/lib/ielts/media backup /scripts/restore.sh latest

docker compose up -d migrate      # brings the schema to head if the backup
                                  # predates a migration. Idempotent.
docker compose up -d
```

`restore.sh` puts the **media back first** — the mirror of the backup order, and
for the same reason: by the time the database is live, every file it references
already exists. It verifies checksums before touching anything, refuses without
the confirmation variable, and keeps the previous media directory at
`media.previous` until you delete it.

Then sit one mock end to end, with audio. A database that restored cleanly and a
paper that plays are different claims, and only the second one is the product.

**Do the drill before you need it.** Restore onto a scratch box and sit a mock
on it. The nightly verification proves the dump restores; it does not prove you
know the steps under pressure.

## Day to day

```bash
docker compose logs -f api               # JSON, one line per request
docker compose exec postgres psql -U ielts ielts
curl -s https://$DOMAIN/metrics/workers  # outbox_lag_seconds
```

`outbox_lag_seconds` is the number to watch: green under 5 s, page over 60 s
sustained. Everything downstream of the relay is asynchronous, including
scoring, so a stuck relay is a centre whose mocks never get marked.

Deploying a change is `git pull && docker compose up -d --build`. Migrations run
in their own one-shot container first; the API waits for it to complete.

## Known gaps

Stated so nobody deploys expecting them:

- **Speaking has no TURN deployment here.** The booking checks are in place —
  age band and parental consent for stranger matching — but no media relay is
  deployed by this repository.
- **Writing has no scoring engine.** Reading and Listening are what the engine
  scores.
- ~~A paid order grants nothing~~ — **fixed.** `_grant_for_order` inserts
  `entitlements` rows from `products.features` on both capture paths, in the
  transaction that marks the order paid.
- **SMS has no provider**, deliberately, and fails closed. See
  [pilot.md](pilot.md).
- **Most realtime frames have no producer.** The gateway is real; five event
  types are wired. Exam timing does not depend on any of it — exam sync is plain
  HTTP, deliberately (ADR §8.2).
