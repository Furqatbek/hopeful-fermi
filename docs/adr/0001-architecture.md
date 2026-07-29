# ADR-0001: Backend Architecture for the IELTS Hub MVP

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Author:** Backend architecture
- **Supersedes:** —
- **Deliverable:** 1 of 5 (ADR). Deliverables 2–5 are gated on approval of this document.

---

## 0. TL;DR

| Decision | Choice | One-line reason |
|---|---|---|
| Language / runtime | **Python 3.12** | Fastest path through the authoring domain; DOCX + stats ecosystem is decisive |
| Web framework | **FastAPI** (sync handlers by default, async only in the realtime gateway) | Pydantic v2 *is* the question-type registry's JSON Schema layer |
| ORM / migrations | **SQLAlchemy 2.0 + Alembic** | Content versioning and item analysis need real SQL, not a query-builder toy |
| Database | **PostgreSQL 16** — sole durable store | JSONB for question payloads, FTS for search, one system to back up |
| Cache / coordination | **Redis 7** — nothing durable lives here | Leaderboards (ZSET), presence, rate limits, WS fan-out |
| Background jobs | **Dramatiq** + Postgres **transactional outbox** | Redis loss must never lose a regrade |
| Realtime | **One WebSocket gateway** for matchmaking + signaling + leaderboards. **Exam sync is plain HTTP.** | See pushback #2 |
| Media | **S3-compatible only** (MinIO in dev, Hetzner/local IDC in prod) | Provider portability is a legal requirement here, not a preference |
| Deploy | **One VPS, Docker Compose, Caddy** | K8s buys nothing at 200 concurrent and costs 4–8 h/month forever |
| Admin backoffice | **sqladmin** over the SQLAlchemy models | The one real thing FastAPI loses vs Django; this recovers ~70% |
| Auth | Short-lived JWT + rotating opaque refresh (90 d), **Telegram-first onboarding**, SMS as fallback | SMS at ~$0.003/msg is the line item that blows your budget |
| Infra bill (your definition) | **≈ $18–32/month** | Excluding TURN + media, as you specified |
| Infra bill (honest total) | **≈ $40–60/month** including TURN, media, SMS | Full breakdown in §3 |
| RPO / RTO | **RPO ≤ 60 s, RTO ≤ 2 h** | With a caveat about live competitions — §10 |

**Headline pushbacks, expanded in §8:** your speaking-matchmaking design has the cold-start problem backwards; exam autosave over WebSocket is worse than HTTP on the networks you're targeting; "no redeploy for a new question type" is achievable for data but not for genuinely novel scoring logic, and pretending otherwise would build you a sandboxed-DSL tarpit; regrade interacts with competition prizes in a way that is a governance problem, not a technical one; and arbitrary-DOCX import is a research project that will eat your MVP.

---

## 1. Context restated in engineering numbers

Everything below is sized against these, and nothing is sized against a hypothetical future.

| Quantity | MVP value | Implication |
|---|---|---|
| Registered users | 500 – 1,500 | A single Postgres table. Not a scaling concern in any dimension. |
| Daily active | 100 – 200 | ~5–15 req/s average, ~50 req/s peak. One process handles this. |
| Peak concurrent (competition) | ~200 | 200 exam sessions × 1 autosave / 10 s = **20 writes/s**. Postgres idles at this. |
| Exam payload size | ~150–250 KB JSON per test | 200 clients × 200 KB in one second = **320 Mbps burst** — the only real spike. §5.6. |
| Listening audio | ~30 min, Opus 48 kbps mono ≈ **11 MB** | 200 tests ≈ 2.2 GB stored. Storage is free; egress is the cost. |
| Total attempts, year 1 | ~50,000 | `attempt_answers` at ~40 rows/attempt = **2 M rows**. Trivial. |
| Concurrent speaking pairs | 200 (your stated peak) | ~30% will need TURN relay ⇒ 60 relayed pairs ⇒ 6 Mbps sustained. §3.2. |

**The single most important number on this page:** your peak load is roughly 1/1000th of what a single modern VPS can serve. Every architectural decision below therefore optimizes for *your hours* and *correctness under bad networks*, and explicitly refuses to optimize for throughput. Where I do spend complexity — the question-type registry, the versioned content model, the regrade path, the authz filter layer — it is because those are the things that are expensive to retrofit, not because they are slow.

---

## 2. Decision: stack

### 2.1 Recommended — Python 3.12 / FastAPI / SQLAlchemy 2.0 / PostgreSQL 16

**Why this and not the others, in order of how much it actually matters for *this* product:**

1. **Bulk import is your highest-leverage feature and it is a document-parsing problem.** You said it yourself: no teacher hand-builds forty questions twice. `python-docx` + `lxml` give you real OOXML access — styles, numbering, tables, inline runs, embedded images — which is exactly what you need to lift a structured test out of a Word file. The JS equivalent (`mammoth`) is a lossy HTML converter, and in Go you are writing an OOXML parser yourself. This is a multi-week delta on the feature you rate highest.

2. **The question-type registry is a JSON Schema problem, and Pydantic v2 is a JSON Schema engine with a Rust core.** Payload schemas, key schemas, and validation rule sets are all `model_json_schema()` in one direction and `TypeAdapter(...).validate_python()` in the other. You get the registry's validation half nearly for free, and the same schemas serialize straight into the OpenAPI document for the frontend.

3. **Item analysis (§ your module 8) is statistics.** Per-question difficulty (*p*-value), discrimination (point-biserial correlation), distractor analysis, and "flag items where everyone fails" are ten lines with `numpy`/`scipy` and a genuine chore anywhere else. You called this "what a centre actually pays for" — the language should not fight you there.

4. **You are one person.** Python's density on CRUD-shaped, validation-heavy, schema-driven domain code — which is most of modules 1, 2, 7 and 8 — is a real multiplier when there is no one to split work with.

**What this choice costs you, honestly:**

- **No Django admin.** This is the genuine loss and I am not going to hide it. You need a backoffice for takedowns, user lookup, audit browsing, and manual entitlement grants. Mitigation: `sqladmin` mounted at `/admin` behind platform-admin auth gives you list/filter/detail/edit over the SQLAlchemy models for roughly a day of work. It is ~70% of `django-admin`. If your backoffice needs grow past that, you write a small internal SPA against the same API — which you need anyway.
- **Raw CPU throughput is 5–20× worse than Go.** At 50 req/s peak this is irrelevant. It becomes relevant at ~2,000 req/s, which is 40× your peak; §D5 will put a number on it.
- **Async/sync discipline.** Addressed by a hard rule in §5.7 rather than by hoping.

**Why not Django, since it is the obvious counter-suggestion:** the free admin is worth real weeks, but (a) Channels for WebSockets is a heavier, more operationally awkward path than Starlette's native WS, (b) DRF serializers fight you when payload shape is data-driven rather than class-driven — which is the entire premise of your registry, and (c) versioned-content queries and item analysis want CTEs and window functions, where SQLAlchemy 2.0 is materially better than the Django ORM. FastAPI + `sqladmin` nets out ahead. This is a close call and I would not argue hard if you overrode it.

### 2.2 Alternative A — TypeScript / Fastify / Drizzle / PostgreSQL

**The real argument for it:** one language across your frontend and backend. For a solo engineer that is not a small thing — no context-switch tax, shared validation schemas (Zod on both sides), shared types for the exam payload, one toolchain. If your frontend is Next.js, this is a defensible override of my recommendation and I would not call it a mistake.

**Honest trade-offs:**

| | Better than Python | Worse than Python |
|---|---|---|
| JSON Schema | `ajv` is the fastest, most complete validator anywhere | — |
| Shared types | Zod schemas shared client↔server; genuinely excellent | — |
| DOCX import | — | `mammoth` is lossy HTML conversion; structured extraction means hand-rolling OOXML |
| Item analysis | — | No `scipy`; you hand-write point-biserial and hope |
| Long-running work | — | Transcode/import orchestration in Node is workable but more awkward |
| Ecosystem churn | — | Your ORM and framework choices will feel dated in 24 months; Python's won't |

Use **Fastify, not NestJS.** NestJS's decorator/DI machinery is team-scaling infrastructure, and you have no team; it is pure overhead for one person. And use **Drizzle, not Prisma** — Prisma's handling of JSONB, recursive/versioned queries, and raw SQL escape hatches is exactly wrong for a content-versioning system.

**Pick this over my recommendation if:** your frontend is already TypeScript *and* you are willing to accept "structured JSON/CSV import only, DOCX later or never" as a v1 constraint. That constraint is the whole trade.

### 2.3 Alternative B — Go / chi / sqlc / PostgreSQL

**The real argument for it:** a single static binary, ~40 MB RSS, deploys as one file, and genuinely excellent at the two concurrency-shaped parts of your system (WebSocket signaling, synchronized competition starts). It would run your peak load on a $5 VPS with the fan off. Operationally the most boring option on this page, which is a virtue you asked me to weight.

**Why I still don't recommend it:** the concurrency advantage applies to maybe 15% of your codebase, and the remaining 85% — authoring, import, validation, permissions, analytics — is precisely where Go's verbosity costs a solo developer the most. Meanwhile:

- DOCX import: you are writing an OOXML parser. Weeks.
- JSON Schema: Go's validator libraries are usable but thin, and the schema-first ergonomics you need for the registry are poor.
- Statistics: you hand-write everything.
- Dynamic, data-driven payloads in a static language means `map[string]any` and runtime assertions everywhere — you pay Go's type-system tax and then opt out of the benefit in exactly the module where flexibility is your stated requirement.

**Pick this over my recommendation if:** you decide the authoring system ships as JSON-import-only with a thin web form, and the product's centre of gravity moves to live competitions at much larger scale than you've described. That is a different product than the one you specified.

### 2.4 Supporting choices

| Component | Choice | Reason |
|---|---|---|
| Job runner | **Dramatiq** + Redis broker, sync workers | Simpler than Celery, sane retries/DLQ, worker code identical to web code |
| Job durability | **Transactional outbox** table in Postgres → relay → Dramatiq | A regrade enqueued in the same transaction as the key change can never be lost |
| Reverse proxy / TLS | **Caddy** | Automatic HTTPS with zero config; nginx + certbot is more moving parts for a solo dev |
| App server | **gunicorn** + uvicorn workers | Boring, battle-tested process supervision |
| Audio transcode | **ffmpeg** subprocess in a Dramatiq worker | Do not use a Python audio library; shell out |
| Search | **Postgres FTS + `pg_trgm`** | No Elasticsearch. Ever, at this scale. |
| Object storage | **S3 API via `boto3`** | MinIO in dev, any S3-compatible provider in prod |
| Error tracking | **Sentry free tier**, or self-hosted **GlitchTip** | Free; GlitchTip if you want errors in-country too |
| Logging | **structlog** → JSON → stdout → rotated file | Grep-able; `trace_id` on every line so you can add OTel later without a rewrite |
| Testing | pytest + `testcontainers` (real Postgres) + `freezegun`/injected clock | Scoring and exam timing cannot be tested against mocks |
| Import linting | **`import-linter`** in CI | This is what makes "modular monolith" a fact rather than an aspiration — §4.3 |

---

## 3. Deployment topology and the honest bill

### 3.1 Topology

```
                         Caddy (TLS, rate limit, static)
                                    │
        ┌───────────────────────────┼──────────────────────────┐
        │                           │                          │
   gunicorn/uvicorn           ws-gateway (async)         sqladmin  /admin
   FastAPI  (sync)            signaling · presence
   N = 2×cores+1              leaderboard push
        │                           │
        └──────────┬────────────────┘
                   │
        ┌──────────┴──────────┬─────────────────┬──────────────────┐
   PostgreSQL 16         Redis 7          Dramatiq workers    S3-compatible
   (sole durable      (cache only,        (transcode,          object store
    store)             disposable)         import, regrade,     (media, backups)
                                           notify, outbox relay)

  Separate small VPS, Tashkent:  coturn (TURN/STUN)   ← media never touches app box
```

All of this is one `docker-compose.yml` on one VPS at MVP. Postgres and Redis are containers on the same box with volume mounts. This is a deliberate choice: it makes the entire production environment reproducible in a single file, which is what makes the data-residency migration in §9.1 a two-hour job instead of a project.

### 3.2 Cost model

Prices are approximate as of writing and should be re-checked; the ratios are what matter.

**Within your stated cap (excluding TURN and media storage):**

| Item | Spec | $/month |
|---|---|---|
| App + DB + Redis + workers VPS | 4 vCPU / 8 GB / 160 GB NVMe (Hetzner CPX31 ≈ €16, or Tashkent IDC equivalent) | 17 – 30 |
| Error tracking | Sentry free tier (5k events/mo) or self-hosted GlitchTip on the same box | 0 |
| Uptime monitoring | UptimeRobot free | 0 |
| Domain | .uz or .com | 1 – 2 |
| **Subtotal** | | **$18 – 32** ✅ |

You asked for under $50 excluding TURN and media. You are comfortably inside it, with headroom to double the VPS before you feel anything.

**TURN, estimated honestly at your stated 200 concurrent pairs:**

Opus voice is ~40 kbps per direction, ~50 kbps with RTP/UDP overhead. A TURN-relayed pair costs the relay ~100 kbps of egress (it forwards both directions). Industry figures put relay fallback at 8–20% of connections; Uzbek mobile networks are heavily CGNAT'd, so assume **30%**.

```
200 pairs × 30% relayed          =  60 relayed pairs
60 × 100 kbps                    =  6 Mbps sustained at peak
6 Mbps × 2 h/day × 30 days       ≈  162 GB/month egress
```

| TURN option | $/month at 162 GB | Verdict |
|---|---|---|
| **Self-hosted coturn**, 2 vCPU / 2 GB VPS in Tashkent | **5 – 10** | ✅ Do this |
| Self-hosted coturn on Hetzner CPX11 (20 TB included) | ~5 | Works, but adds ~120 ms RTT for two peers who are both in Tashkent |
| Twilio Network Traversal @ ~$0.40–0.60/GB | **65 – 100** | ❌ 10× the cost for zero benefit |
| Cloudflare Calls / managed TURN | varies | Revisit at 1,000+ concurrent pairs |

**Put coturn on a Tashkent VPS.** Both peers are in-country; relaying their audio through Finland is 120+ ms of pointless latency on a *speaking practice* product where conversational turn-taking is the entire point. This also sidesteps the residency question for real-time media, which you are not storing anyway.

Bandwidth headroom note: 6 Mbps sustained is nothing, but coturn is UDP-heavy and some budget VPS providers shape UDP aggressively. Verify UDP throughput before committing to a host.

**Media storage and egress:**

```
Storage:  200 tests × 11 MB + images    ≈ 3 GB          →  < $1/month
Egress:   200 DAU × 0.3 listening/day × 11 MB × 30      ≈  20 GB/month
```

| Item | $/month |
|---|---|
| S3-compatible storage + egress (Hetzner Object Storage ≈ €6 for 1 TB + 1 TB traffic, or Backblaze B2) | 6 – 10 |

**A tension worth naming now:** per-user tokenized audio URLs (your anti-scrape requirement) mean **zero CDN cache hits** — every byte is an origin fetch. At 20 GB/month that is irrelevant and you should simply serve audio through your own app with a signed, single-use token and HTTP range support. Revisit only when monthly audio egress crosses ~500 GB. Do not put a CDN in front of tokenized media and imagine you are getting cache hits; you are not.

**The line item you did not budget for — SMS.**

```
1,500 users × ~4 OTP/month ≈ 6,000 SMS
Uzbek gateways (Eskiz.uz, Play Mobile): roughly $0.002–0.005 per message
                                        ≈ $12–30/month  ⚠️
```

That is up to **the same order as your entire stated infra budget**, and it grows linearly with users while everything else on this page stays flat. It is the first thing that will break your unit economics. See pushback #6 for the fix.

**Honest total: ~$40–60/month all-in at MVP**, of which the part you asked me to fit under $50 is $18–32.

---

## 4. Module boundaries

### 4.1 The map

```
app/
  platform/                 # shared kernel — infrastructure only, zero domain logic
    config/ db/ logging/ errors/ ids/ jobs/ outbox/ storage/ clock/ ratelimit/ events/
  modules/
    identity/               # users, orgs, memberships, cohorts, OTP, tokens, consent
    authz/                  # central policy engine — the permission matrix lives here alone
    qtypes/                 # question-type REGISTRY: schemas, validators, scoring strategies
    content/                # authoring: tests, sections, passages, audio, groups,
                            #   questions, keys, versions, lifecycle, import/export, media
    exam/                   # attempt lifecycle, server clock, autosave, submit, score, regrade
    competitions/           # schedules, entries, leaderboards, anti-cheat signals
    speaking/               # presence, queue, matching, signaling, scheduled slots
    safety/                 # reports, blocks, mutes, age policy, immutable audit log
    billing/                # payment ports (Click, Payme), orders, entitlements
    analytics/              # read models: cohort progression, item analysis
  api/                      # HTTP + WS routers. Thin. DTO ↔ service calls only.
  workers/                  # Dramatiq actors. Thin. Calls module services only.
```

Every module has the same internal shape, and this uniformity is the point:

```
modules/<name>/
  service.py     # THE public surface. Other modules may import only this and schemas.
  ports.py       # Protocols this module needs FROM others (dependency inversion)
  schemas.py     # Pydantic DTOs crossing the boundary
  models.py      # SQLAlchemy ORM — PRIVATE. Importing this cross-module is a CI failure.
  repo.py        # Data access — PRIVATE.
  events.py      # Domain events this module emits
  policies.py    # Module-specific authz rules, REGISTERED with authz (not enforced locally)
```

### 4.2 Allowed dependency edges

```
api, workers   →  any module (composition root)
competitions   →  exam, content(read), identity, authz, safety
exam           →  qtypes, content(read), identity, authz, billing(entitlements), safety(audit)
content        →  qtypes, identity, authz, safety(audit), platform.storage
speaking       →  identity, authz, safety, content(read: cue cards)
billing        →  identity
safety         →  identity
analytics      →  nothing at runtime (fed by domain events into its own read models)
identity, qtypes, authz  →  platform only
```

The graph is acyclic. `analytics` reading nothing is deliberate: it is the module most likely to want its own replica or process later, so it never gets to depend on another module's tables.

### 4.3 How the boundaries are actually enforced

Boundaries that live only in a document are decoration. Three mechanisms, all in CI:

1. **`import-linter` contracts** in `setup.cfg`: a layered contract encoding the graph above, plus a forbidden contract — no module may import `modules.*.models` or `modules.*.repo` from outside itself. This fails the build, not a code review.
2. **One test asserts the module list and dependency graph match this ADR.** When you intentionally add an edge, you update both, and the diff makes you think about it for ten seconds. That is the entire mechanism, and it is enough.
3. **Separate SQLAlchemy metadata per module, one Alembic history.** Cross-module foreign keys are permitted (you are one database and pretending otherwise is theatre) but must reference another module's *root* entity only — `user_id`, `org_id`, `test_version_id` — never its internal tables. This is what keeps a future extraction to a separate service a week of work rather than a rewrite.

### 4.4 The interfaces that matter

These five ports are load-bearing. The rest is ordinary code.

```python
# authz — the whole permission matrix, enforced in one place
class Policy(Protocol):
    def check(self, actor: Principal, action: Action, resource: ResourceRef) -> Decision: ...
    def filter(self, actor: Principal, action: Action, query: Select) -> Select: ...
```
`filter` is not an afterthought — it is the half that keeps your contractual promise. `check` prevents a teacher from *opening* a competitor centre's test; `filter` prevents that test from appearing in a *list* response in the first place. Almost every real multi-tenant data leak is a missing list-scope, not a missing detail-check. Every query that returns content passes through `filter` or it does not ship.

```python
# content — the exam engine NEVER sees mutable authoring models
class ContentReadPort(Protocol):
    def get_published_version(self, version_id: TestVersionId) -> TestVersionSnapshot: ...
    def get_media_grant(self, user_id, version_id, asset_id) -> SignedMediaGrant: ...
```
An immutable snapshot at the boundary is what makes "attempts bind to the exact version the student sat" structurally true rather than a rule people remember to follow.

```python
# qtypes — the registry; the exam engine's ONLY knowledge of question types
class QuestionType(Protocol):
    def validate_payload(self, payload: dict) -> list[ValidationError]: ...
    def validate_key(self, payload: dict, key: dict) -> list[ValidationError]: ...
    def blanks(self, payload: dict) -> list[BlankRef]: ...
    def score(self, response: dict, key: dict, rules: GroupRules) -> ItemScore: ...

class Registry(Protocol):
    def get(self, type_key: str, schema_version: int) -> QuestionType: ...
```

```python
# billing — one call site for every entitlement question in the product
class Entitlements(Protocol):
    def check(self, user_id, feature: Feature, ctx: EntitlementCtx) -> Entitlement: ...
```

```python
# platform — injectable clock; the exam module MUST NOT call datetime.now()
class Clock(Protocol):
    def now(self) -> datetime: ...
```
Without this, "the server is the sole authority on time remaining" is untestable, and untested time logic is how you lose a competition.

### 4.5 Events

A ~100-line in-process event bus. Two delivery modes:

- **Synchronous, same transaction** — for consistency-critical reactions (audit log writes, exposure counters).
- **Asynchronous via the outbox** — for everything else (regrade fan-out, notifications, analytics projections, webhook retries).

The outbox row is written in the *same transaction* as the domain change. A relay process polls and dispatches to Dramatiq. This is the entire reason you do not need Kafka: you get at-least-once delivery with transactional consistency for about 80 lines of code and zero additional infrastructure. It is also the seam along which you would later extract a service, if you ever needed to.

---

## 5. Cross-cutting decisions

### 5.1 Identifiers: internal `bigint`, external opaque

Internal PKs are `bigint GENERATED BY DEFAULT AS IDENTITY` — small indexes, fast joins, good locality. Externally exposed entities additionally carry an `xid` (UUIDv7, stored as `uuid`), and the API speaks *only* `xid`.

This is not aesthetics. `/api/tests/1234` is a scraping API: an attacker walks the integer space and enumerates your centres' entire content library, which is precisely the contractual breach you are most exposed to. Sequential IDs would silently undermine module 2h. The cost is one translation at the API boundary, and it is worth paying.

### 5.2 Auth

- Access token: JWT, 15 min, `sub` = user xid, carries active org context + role.
- Refresh token: **opaque, random, stored hashed in Postgres, 90 days, rotating on use, revocable.** Opaque because a safety ban must kill a session *now*; you cannot revoke a stateless JWT.
- The 90-day refresh lifetime is a direct cost decision: every forced re-login is an SMS you pay for. Long sessions are cheaper *and* better UX on flaky networks.
- OTP: 6 digits, 5-minute TTL, max 5 attempts, hard rate limits per phone and per IP, constant-time compare, and the code is never logged.

### 5.3 Time

The server is the only clock. Attempt deadlines are computed server-side and stored absolute (`expires_at`). The client receives `server_now` + `expires_at` on every sync response and renders a countdown from the delta — never from its own clock. Submissions past `expires_at + grace` are accepted but marked `late` with the overrun recorded, because on the networks you are targeting a 4-second-late submission is a network hiccup, not cheating. The grace window is configurable per assignment and defaults to 30 s; the anti-cheat module sees the overrun, the scoring engine does not.

### 5.4 Storage

Every media reference is `(bucket, key, content_type, bytes, checksum)` — never a URL, never a provider-specific handle. Access is exclusively through `platform.storage`, which exposes `put`, `get`, `presign(ttl)`, and `delete`. Swapping providers is a config change.

### 5.5 Config

12-factor, `pydantic-settings`, one `.env`. No secrets in the repo. No provider-specific SDK outside `platform/`.

### 5.6 The competition thundering herd

200 clients hitting one endpoint in the same second, each pulling ~200 KB, is a 320 Mbps burst. That is the only genuinely spiky thing in this system, and it is worth designing for explicitly:

1. **T−120 s → T−10 s, "lobby":** clients fetch the exam payload **AES-GCM encrypted**, with per-client jitter spreading the fetch over 110 seconds. The blob is rendered once and cached in Redis, so this is a static-file serve.
2. **T−0:** clients request the decryption key. That response is ~100 bytes. 200 of them is a rounding error.
3. Server-side timing starts from the competition's `starts_at`, not from when the client asked for anything.

The encryption is load-bearing for fairness: without it, the payload sitting on the device from T−120 s is a two-minute reading head start for anyone who opens devtools. The whole mechanism is ~40 lines. If you want to skip it in v1, the fallback is serving from a Redis-cached blob at T−0 with 30 s of jitter — that works fine at 200 and starts hurting at ~2,000.

### 5.7 Async/sync rule

One rule, no exceptions: **`async def` only in the WebSocket gateway and in media streaming endpoints. Everything else is `def` and runs in the threadpool.** Sync handlers must not block for more than 200 ms; anything longer is a Dramatiq job. This eliminates an entire category of "I accidentally called blocking I/O in an event loop" bugs, which is a real risk when one person is writing all of this at speed. At 50 req/s peak the threadpool is not remotely a bottleneck.

---

## 6. Explicit arguments against the things you told me to argue against

You asked me to argue rather than just comply. Here is the case, priced in the currency that matters to you: your hours.

| Technology | Setup | Ongoing | Extra $/mo | What it would buy you at 200 concurrent | Verdict |
|---|---|---|---|---|---|
| **Kubernetes** | 20–40 h | 4–8 h/mo | 40–100 | Nothing. You have one box and no scaling event. | **No.** Not at 50k users either. |
| **Microservices** | 30–60 h | 6–12 h/mo | 20–60 | Nothing. Service boundaries solve *team* coordination; you have no team. You would trade in-process function calls for network calls, distributed transactions, N deploy pipelines, and N log streams. | **No.** The modular monolith with enforced imports gets you the same boundaries at zero runtime cost. |
| **Kafka / Redpanda** | 8–16 h | 2–4 h/mo | 20–40 (RAM) | Nothing. Your peak event rate is ~20/s. Postgres outbox + Dramatiq handles four orders of magnitude more. | **No.** Revisit above ~5,000 events/s sustained. |
| **Service mesh** | 10–20 h | 2–4 h/mo | 10–30 | Nothing — there is no service-to-service traffic to mesh. | **No.** |
| **Event sourcing** | +30–50% on every write path, forever | permanent schema-evolution tax | 0 | This one is *almost* justified, so it gets its own paragraph. | **No — see below.** |

**Event sourcing deserves a real argument, because your requirements genuinely gesture at it.** You need: regrade of historical attempts, an immutable audit trail, and attempts bound to exact content versions. Those are the three things event sourcing is famous for, so the pull is real.

But you can have all three for near-zero cost with three ordinary design choices:

1. **Store raw responses separately from derived scores.** `attempt_answers` is append-only and immutable; the student's keystrokes are facts.
2. **Version the things that turn responses into scores** — answer keys and band maps — and record which versions a given scoring run used.
3. **Make scoring a pure function:** `score = f(responses, key_version, band_map_version, rules_version)`, executed by a `score_runs` record that is itself immutable.

Now a regrade is: create a new `score_run` over the same immutable responses with the corrected key version, diff against the previous run, notify the deltas. You get full recomputability, full auditability, and a complete history of *why* every score was what it was — which is the actual business requirement — without turning every write path in your application into an event stream, and without the schema-evolution tax that makes event-sourced systems miserable to change in year two. Add an append-only `audit_log` table for the human-action trail and you have covered the last of it.

**The general principle I am applying:** every one of these technologies solves a coordination problem between *teams* or a throughput problem at a scale you are 1,000× away from. You have neither problem. What you do have is a hard time budget and a product whose value is in the authoring domain. Spend your hours there.

---

## 7. What I am deliberately NOT building at MVP

Each of these is a decision, with the reason, and — where it matters — the seam that keeps the door open.

| Not building | Why | Seam left behind |
|---|---|---|
| **Writing & Speaking band scoring** | You did not ask for it, and AI band scoring that a school will trust is a product in itself. But note: **you are shipping an "IELTS hub" that covers 2 of the 4 skills.** Centres will ask. | `section.skill` enum includes `WRITING`/`SPEAKING` from day one; the registry can host essay-type items later without a migration. |
| **AI speaking partner** | Agreed, per your spec. | `speaking.MatchProvider` port — a bot partner is another implementation. |
| **Content marketplace** | Correct call to defer. | `content.owner_org_id`, `visibility`, and a `license` field exist in the schema now; listing/pricing is additive. |
| **Native mobile apps** | A PWA reaches your users at 1/10th the cost. | API is mobile-first: idempotent writes, resumable everything, small payloads. |
| **Proctoring (webcam, screen lock, lockdown browser)** | Expensive, defeatable, invasive, and **legally fraught with minors' video**. Recording minors' webcams creates a data-protection liability that dwarfs the cheating it prevents. | Telemetry-based signals only (§module 4). Room to add a "proctored" attempt flag later. |
| **Real-time collaborative authoring** | Google-Docs-style co-editing is weeks of CRDT work for a problem two teachers hit twice a year. | Optimistic locking (`version` column) + draft autosave + a clear "someone else edited this" conflict screen. |
| **Full i18n framework** | But **do** externalize strings now — uz-Latn and ru are both non-negotiable in this market and retrofitting is miserable. | `user.locale`, all transactional strings (SMS/Telegram/email) in catalogs from commit one. UI strings ship uz/ru/en. |
| **Custom/granular RBAC** | Four fixed roles + content scopes covers every case you described. Custom role builders are enterprise features. | Actions are named constants in `authz`; a role→action mapping table is additive. |
| **Elasticsearch** | Postgres FTS + `pg_trgm` handles a few thousand tests. | — |
| **Separate admin SPA** | `sqladmin` costs a day. | — |
| **Multi-region / HA / hot standby** | One box + tested restore. Accept the RTO (§10). | Compose file makes standing up a second box mechanical. |
| **SSO/SAML for schools** | No Uzbek prep centre is running an IdP. | Centre admin invites by phone. |
| **Full offline exam delivery (service worker)** | Tempting on these networks, but caching an entire test offline is both a content-leak vector and a large sync-conflict surface. | **Do build the client answer outbox** (IndexedDB, ~200 LOC) — that is 90% of the resilience benefit at 5% of the cost. |
| **Video in speaking practice** | Audio-only halves TURN bandwidth *and* materially reduces the safety surface with minors. This is my recommendation, not just a deferral. | WebRTC negotiation is media-agnostic; enabling video later is a constraint change. |
| **Routine session recording** | You said audio should not transit or be stored, and you are right — mass recording of minors' voice conversations is a data-protection problem you do not want. | **Client-side rolling 60-second buffer, uploaded only when a user files a report.** Evidence for safety cases without mass surveillance. Requires clear notice at session start. |

---

## 8. Where I think you are wrong

You asked me to push back. Nine items, roughly in order of how much they will cost you if I am right and you proceed anyway.

### 8.1 Speaking matchmaking: the cold start isn't a cold start, it's the steady state

You framed scheduled fixed-slot sessions as cold-start *support*, with the live queue as the primary mechanism. Run the numbers: 150 DAU spread across a 16-hour day, with maybe 10% interested in speaking practice on a given day, is **~1 interested user per hour**. A live matching queue where the median wait is 40 minutes is not a feature, it is a graveyard — and every user who queues, waits, and leaves is a user who does not come back to that tab.

**Invert it.** Scheduled slots are the *product*: fixed daily sessions (say 19:00, 20:00, 21:00 Tashkent), users book in advance, you get a Telegram reminder 15 minutes out, and matching runs as a **batch at slot open** against everyone who showed up. Batch matching on a pool of 20 gives you good band/language pairing; greedy real-time matching on a pool of 1 gives you nothing. The live queue ships as a secondary "try now" that works during peak hours only, and you show an honest expected wait rather than a spinner.

This is a smaller build than the real-time queue, and it is the difference between the feature working at 150 DAU and not working until 5,000. The matching algorithm itself is the same code — it just runs on a batch instead of a stream.

### 8.2 Exam autosave over WebSocket is worse than HTTP on the networks you're targeting

You listed "exam sync" alongside matchmaking and leaderboards as realtime. I would not use a WebSocket for the exam.

You have stated that mobile connections drop constantly and that losing answers is unacceptable. Those two facts argue *against* a stateful long-lived connection:

- A dropped WS means reconnect, re-auth, resubscribe, and resolve "what did the server actually receive?" — you are hand-rolling a reliable-delivery protocol over a transport that pretends to be reliable but isn't.
- A dropped `POST /attempts/{id}/answers` with an idempotency key means the client retries and the server deduplicates. That is it. That is the whole failure-handling story.
- HTTP survives captive portals, mobile carrier proxies, and aggressive middleboxes that silently eat idle WebSockets — all common on Uzbek mobile networks.
- Battery and radio behaviour is better: batched HTTP posts let the radio sleep; an open WS with keepalives does not.

**Design:** client keeps an IndexedDB outbox, flushes every 5–10 s or on blur, `POST` batched answer deltas with `Idempotency-Key`, server responds with `{server_now, expires_at, accepted_seq}`. That response *is* your clock sync, so you get server-authoritative timing for free on every save. WebSockets stay for matchmaking, signaling, and leaderboard push — places where the server genuinely needs to initiate.

I will still specify the full realtime contract in Deliverable 3, including exam events, because you asked for it and there is a legitimate use (a proctor watching a cohort's live progress). But the student's answer path should be HTTP.

### 8.3 "No redeploy for a new question type" is half achievable, and the other half is a trap

Adding a question type without a **migration** is entirely achievable, and Deliverable 2's acceptance test will demonstrate it end to end. Adding one without a **redeploy** is achievable for any type whose scoring composes from existing primitives — which, and I want to be precise here, is **every single type on your MVP list.** All 19 of them decompose into: `exact_set_match`, `ordered_sequence_match`, `text_normalize_and_compare`, `one_of_accepted`, `partial_credit_over_blanks`, and a small set of normalizers (case, whitespace, articles, spelling variants, number-word forms, hyphenation).

Where I push back is on the implied general case: *genuinely novel scoring logic is code*, and the only way to add arbitrary code without a deploy is a sandboxed DSL or plugin runtime. For a solo engineer that is a security surface (untrusted code execution, resource exhaustion) and a maintenance sink (you now own a language), and it would consume weeks that belong to the import pipeline.

**The honest contract, and the one I recommend you commit to:**

| Change | Requires |
|---|---|
| New type composing existing scoring primitives | **Nothing but data.** Insert a registry row. No migration, no deploy. |
| New *normalizer* (e.g. Cyrillic/Latin transliteration tolerance) | ~20 lines + deploy. Rare. |
| New *scoring primitive* (e.g. drag-order with partial credit by adjacency) | ~30–50 lines + deploy. Rarer. |

If I promised you "any conceivable future question type, zero deploy," I would be selling you a plugin architecture you would have to maintain forever. The registry is data plus a small, closed set of composable primitives — and that is the design that actually earns the flexibility you are paying for.

### 8.4 Regrade × competitions is a governance problem you haven't specified

Your regrade requirement is correct and I fully agree it is non-optional. But consider: a competition ran in March, prizes were awarded, a teacher fixes a bad key in June. Do the March rankings change? Does a student who placed 4th and now scores into 3rd get the prize?

Recomputing is trivially easy technically and potentially disastrous socially. My recommendation:

- **Competitions freeze the key version at start.** Practice and assignment attempts regrade automatically and silently-ish (with notification). Competition results **do not** auto-regrade.
- A post-hoc key fix on a competition item generates an **admin decision task**: "3 items affected, 12 rankings would change, 1 podium position moves." A platform admin explicitly chooses `leave as-is` / `regrade and republish with public notice`.
- Either choice writes an audit record with the admin's identity and rationale.

You need a written policy for this before your first paid competition, and the API needs to model both outcomes. This is squarely in "will lose you a school client" territory — arguably more so than a bad key, because a silently-changed leaderboard looks like fraud.

### 8.5 Arbitrary-DOCX import will eat your MVP

"Import a complete test from a structured DOCX" is the right feature and I agree it is your highest-leverage item. But there is a hidden ambiguity: parsing a **template you provide** is a two-week feature; parsing **whatever Word file a teacher already has** is an open-ended research project with no definition of done.

**Architecture that keeps this honest:**

```
DOCX ─┐
CSV  ─┼──► adapter ──► canonical Import JSON ──► validator ──► dry-run diff ──► commit
JSON ─┘                    (the contract)
```

The canonical **Import JSON is the contract**, and it is what you specify, test, version, and export. DOCX and CSV are *adapters* that produce it. This means: the whole validate/dry-run/diff/commit pipeline is tested against JSON and works identically for every source; a bad DOCX adapter degrades to a partial parse with a clear error report rather than corrupting a test; and you can ship the JSON path in week one and improve DOCX parsing forever without touching the risky part.

Ship a **locked .docx template** with named styles/markers, and be explicit with centres that this is the supported path. "Upload any Word document" is a v3 aspiration; promising it in a sales conversation now will cost you support hours for years.

### 8.6 SMS will quietly break your unit economics — use Telegram

At $0.002–0.005 per SMS and ~4 OTPs/user/month, SMS is a **per-user cost that scales linearly** while everything else on your infra bill is flat. At 20,000 users that is $160–400/month, and it is the only line item on your entire architecture that grows with success.

Telegram penetration among 15–25-year-olds in Uzbekistan is close to universal. So:

- **Primary onboarding: a Telegram bot / Mini App using `request_contact`.** The user shares their phone through Telegram, you receive it already attached to a verified Telegram account, and you **skip OTP entirely.** Free, and it is a better UX than typing a code.
- **Fallback: SMS OTP** for users without Telegram. Keep this path fully working — do not make Telegram a hard dependency for login, both because coverage is not 100% and because messenger availability in the region is not guaranteed to be stable.
- **Bonus, and this is not small:** a Telegram ID is a strong second identity signal for your duplicate-account detection in module 4 (SIM cards are cheap here; Telegram accounts are stickier). And it is a **free notification channel** for regrade notices, competition reminders, and assignment deadlines — which is where the rest of your SMS spend would otherwise go.

Estimated effect: 70–80% reduction in SMS volume, and a materially better signup funnel. This is probably the highest-ROI item in this entire document relative to its implementation cost.

### 8.7 Minors in 1:1 voice chat with strangers is your highest-risk feature, and age banding isn't enough

Your spec says minors must never be matched 1:1 with adults, enforced at the matching layer. Correct, necessary, and insufficient. Two 15-year-olds in an unmoderated 1:1 voice call with no recording is still a safeguarding surface, and "we checked the ages" is not a defence anyone will accept if something happens.

**I recommend going further than your spec:**

- For under-18 accounts, **random stranger matching is OFF by default.** Minors may match within their own organization/cohort (people their teacher knows) or in **scheduled, teacher-visible slots**.
- Random matching for a minor requires an explicit **parental consent flag** — which you are already modelling — plus a separate, specifically-worded opt-in for stranger matching. One consent checkbox covering "use of the platform" does not cover "voice calls with strangers," and a regulator will not read it that way either.
- **Audio-only**, never video, for any session involving a minor. (I would do audio-only for everyone at MVP.)
- Every session gets a `safety_session` record: participants, times, and the report path pre-wired. The rolling 60-second local buffer (§7) is uploaded only on report.
- Reports involving a minor route to a **separate, higher-priority admin queue** with a defined response SLA that you actually write down.

This is the feature most likely to generate a story you cannot recover from. Build the constraints before the feature, not after the incident.

### 8.8 You are missing a payments prerequisite with weeks of lead time

Click and Payme both require a **registered legal entity** (OOO or self-employed/YaTT status) with a corporate bank account, plus a merchant onboarding process. That is typically weeks of paperwork, not days, and it is entirely outside your control.

Also, a specific technical warning: **do not design a naive one-phase `charge()` port.** Payme drives a JSON-RPC merchant API where *they* call *you* with a transaction state machine (`CheckPerformTransaction` → `CreateTransaction` → `PerformTransaction`, plus `CancelTransaction`, `CheckTransaction`, `GetStatement`). Click uses a two-step `Prepare` → `Complete` callback flow. Both are **two-phase and inbound-driven**. A port modelled as "call provider, get result" will not fit either of them, and you will end up rewriting it.

**Model the port as a state machine from the start:** `initiate → authorized → captured | cancelled | failed`, with idempotent inbound webhook handling keyed on `(provider, provider_txn_id, state)`, and a `GetStatement`-style reconciliation job that pulls the provider's ledger daily and diffs it against yours. You said webhooks arrive twice, late, or never — the reconciliation job is what handles "never," and it is the piece people skip and then regret. Method names above should be verified against current provider docs; the *shape* is the durable part.

**Start the legal-entity paperwork now,** in parallel with development. It is the longest pole in your critical path and it does not care how fast you code.

### 8.9 Two smaller things

**"No bulk-read API for question payloads" is not really achievable, and that is fine.** The exam engine inherently bulk-reads an entire test — that is what an exam is. What actually protects you is: authorization + `filter` scoping, short-TTL single-use media tokens, per-user rate limits on content endpoints, exposure logging with anomaly alerts, and opaque IDs (§5.1). The absence of a "bulk" endpoint is cosmetic; a determined scraper drives the exam endpoint. Design for *detection and evidence*, not prevention — you cannot win prevention against a logged-in user who is allowed to see the content.

**Your word-limit rule has domain subtleties the scorer must encode.** "NO MORE THAN TWO WORDS AND/OR A NUMBER" in real IELTS marking means: hyphenated compounds count as one word; contractions count as one; a violation marks the answer **wrong outright** rather than truncating it; and numbers written as digits count as the "number" allowance. Getting this wrong produces silently incorrect scores that nobody notices for months. It goes in the test suite in Deliverable 4 with explicit cases for each rule.

---

## 9. Legal, safety and data-protection flags

These are flagged as requested. I am an engineer, not your lawyer — but each of these has a concrete architectural consequence, which is why they are in an ADR.

### 9.1 Data residency — the one that constrains the architecture

My understanding is that Uzbek law (the Personal Data law, ZRU-547, as amended in 2021) requires personal data of Uzbek citizens to be processed in databases physically located in Uzbekistan, with registration in a state register, and that there has been enforcement activity against non-compliant services. **Verify the current position with a local lawyer before signing a B2B contract** — but design as if it is true, because the cost of designing for it is nearly zero and the cost of retrofitting is a migration under legal pressure.

**Architectural consequences, which are the reason this is decision #1 and not an afterthought:**

- **No managed-cloud lock-in. None.** No Supabase, no Vercel Postgres, no Firebase, no AWS-specific services, no managed queue. Every dependency must be something you can `docker compose up` on a Tashkent VPS.
- Personal data is concentrated in `identity`, `exam.attempts`, and `safety` — so a residency migration is scoped, not global.
- Media (audio, images) is not personal data and can live anywhere S3-compatible.
- **Decision rule: host wherever is fastest to start, but move PII in-country before your first paid B2B contract.** A school signing a contract is exactly when someone starts asking where the children's data lives. Budget a day for the migration and test the restore path first (§10).

### 9.2 Minors' personal data

- **Collect the minimum.** Phone, given name, date of birth, locale. **Do not collect PINFL or passport data** — you do not need it, and holding it converts a minor incident into a major one.
- DOB is required (you need the 18 boundary for age banding) but is sensitive: never in logs, never in B2B analytics exports, never in leaderboards. Cohort analytics exports carry aggregates and pseudonymous IDs, not birth dates.
- Parental consent is a **flag with provenance**: who consented, when, through what channel, and what version of the consent text. A boolean alone is not evidence.
- Full-disk encryption on the VPS, restricted DB access, and audited admin reads of personal data.

### 9.3 Copyright — assume they will upload Cambridge papers, because they will

You anticipated this, which is good. Let me sharpen the exposure: because you have *predicted* it, a "we had no idea" defence is weaker. Design accordingly.

- **Attestation at upload**, versioned text, logged with uploader identity, timestamp, IP, and asset checksum. Attestation is per-upload, not a one-time account setting.
- **Organization-private by default** (as you specified) — and critically, **content can never transition to platform-global without an explicit platform-admin review action.** That single rule is most of your containment.
- **Takedown flow**: report → immediate soft-hide (invisible, not deleted) → review → decision → audit record. Preserve the evidence; never hard-delete disputed material until the matter closes.
- **Terms of service must place the warranty on the uploading centre, with indemnity.** Get a local lawyer to draft the B2B contract and the ToS. In Tashkent this is a few hundred dollars one-time and it is the cheapest insurance in this entire document.
- **Trademark, separately:** "IELTS" is a registered trademark (British Council / IDP / Cambridge). Descriptive use — "IELTS preparation platform" — is generally defensible; using it *in your product or company name* invites a letter. Name the product something else and describe what it does.

### 9.4 Content integrity is detection, not prevention

Restating §8.9 because it is a policy decision, not just a technical one: against a logged-in user who is authorized to see content, you cannot prevent exfiltration. You can make it slow, make it visible, and make it evidenced. Exposure tracking per item, anomaly alerts on abnormal fetch patterns, opaque IDs, short-TTL per-user media tokens, and rate limits — that is the achievable set, and it is enough to detect a centre systematically harvesting your library and to support terminating them under the contract.

---

## 10. Backups, RPO and RTO

**Designing for RPO ≤ 60 seconds, RTO ≤ 2 hours.**

| Mechanism | Detail |
|---|---|
| WAL archiving | `pgBackRest` → S3-compatible storage, `archive_timeout = 60s` ⇒ **RPO ≤ 60 s** |
| Base backup | Nightly full, 7 daily + 4 weekly + 3 monthly retained |
| `synchronous_commit` | Left at `on` — a committed answer is on disk before the client is told it was saved |
| Media | Object storage with versioning enabled; media is immutable once written |
| Restore drill | **Quarterly, to a scratch VPS, timed and written down.** A backup you have not restored is a hypothesis. |
| Restore runbook | In-repo, `docs/runbooks/restore.md`, step-by-step, no tribal knowledge |

**Two honest caveats:**

1. **A 2-hour RTO means a live competition is lost.** With 200 students mid-contest, a two-hour outage ends that contest — no restore strategy fixes that. Mitigations that actually work at your budget: schedule competitions when you can be present, keep a snapshot taken immediately before each competition (a one-line pre-flight job), and have a written "competition aborted" policy with a rerun/refund path. Buying real HA costs more than your entire infra budget and is the wrong trade at this stage — but you should make that trade knowingly.

2. **The client outbox meaningfully improves the *effective* RPO for answers.** After a restore, clients still holding unacknowledged answers in IndexedDB replay them with their idempotency keys, and the server accepts them. For the one thing you called unacceptable to lose, effective data loss approaches zero even in a restore scenario. This is a second, independent reason to build the outbox in §8.2.

---

## 11. Assumptions

Stated rather than asked, per your instruction. Correct me on any of these and I will adjust before Deliverable 2.

1. Frontend is a web PWA (React/Next or similar) that you own; no native app at MVP.
2. Users are overwhelmingly on mobile, Android-dominant, on 3G/4G of variable quality.
3. Interface languages needed: Uzbek (Latin) and Russian, with English for exam content. English-only UI is not viable for B2C here.
4. You can obtain a Tashkent VPS with reliable NVMe and unshaped UDP; if not, start on Hetzner and plan the residency migration.
5. Reading and Listening only at MVP. Writing/Speaking scoring is modelled in the schema but has no engine.
6. Speaking sessions are audio-only.
7. Competitions are scheduled by you or by centre admins — no user-created contests at MVP.
8. B2B seat licences are annual, invoiced offline (bank transfer is normal for Uzbek schools); Click/Payme handle B2C. The entitlement model is identical for both; only the payment path differs.
9. You will register a legal entity for payment onboarding; that work starts in parallel, now.
10. One production environment plus a staging environment on the same box (separate compose project, separate DB). Not a separate VPS.
11. Time zone is Asia/Tashkent (UTC+5, no DST). All timestamps stored UTC, rendered local.

---

## 12. Suggested build order

Not a deliverable you asked for, but the sequencing matters more than any individual decision here, so: this is the order in which the risk actually retires.

| Phase | Contents | Why here |
|---|---|---|
| **0** | `platform` kernel, identity + Telegram/SMS auth, authz skeleton, CI, deploy, backups, restore drill | Nothing else is safe to build on an untested restore path |
| **1** | `qtypes` registry + scoring engine, **tests first** | This is the acceptance test for the whole design. If the registry is wrong, everything downstream is wrong. |
| **2** | `content` authoring core: hierarchy, versioning, lifecycle, publish gate | The product's actual moat |
| **3** | **Import/export pipeline** (canonical JSON first, then DOCX adapter) | Highest leverage per hour. Nothing else makes a centre productive on day one. |
| **4** | `exam` engine: sessions, timing, autosave, submit, score, **regrade** | Now there is content to sit |
| **5** | `billing` + entitlements | Nothing is sellable until this exists |
| **6** | `analytics` for the B2B dashboard | This is what closes the second and third centre |
| **7** | `competitions` | Growth/retention feature; needs a working exam engine underneath |
| **8** | `speaking` (scheduled slots first, live queue second) + `safety` hardening | Highest risk, lowest MVP leverage. Safety constraints ship *with* it, not after. |

One deliberate note: `safety` primitives (audit log, age banding, report model) land in phase 0 alongside identity, even though the speaking feature is last. The audit log is needed by content and regrade from phase 2 onward, and age banding must exist before any matching code is written.

---

## 13. Consequences

**Positive**

- Single deployable artifact; one `docker compose up` reproduces production anywhere, which is what makes the residency migration cheap.
- Module boundaries enforced by CI rather than discipline, so extraction to services remains possible without ever being required.
- Scoring is a pure, testable function over immutable inputs — regrade, audit, and item analysis all fall out of that one property.
- Infra cost is flat with respect to users up to roughly 20–50k; the only user-linear cost is SMS, and §8.6 largely removes it.

**Negative, accepted**

- Single point of failure. 2-hour RTO. Accepted at this stage; §10 names the specific scenario where it hurts.
- Python throughput ceiling — quantified with thresholds in Deliverable 5.
- No admin framework out of the box; `sqladmin` covers most of it, and some backoffice work is real cost.
- The registry's "no deploy" promise is bounded to composable primitives (§8.3). Stated explicitly so it is not discovered later.

---

## Deliverable 1 ends here.

**Decisions I need from you before Deliverable 2 (the data model):**

1. Stack: confirm Python/FastAPI, or override to TypeScript (§2.2) / Go (§2.3).
2. Telegram-first authentication (§8.6) — this changes the identity schema, so I need it settled before I write the migrations.
3. Scheduled-slots-first speaking (§8.1) — changes the `speaking` schema.
4. Competition regrade policy (§8.4) — freeze-and-review, or auto-regrade.
5. Audio-only speaking and minors-default-to-cohort-matching (§8.7) — confirm or override.

Silence on any of these means I proceed with my recommendation and note it as an assumption.
