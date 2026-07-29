# Deliverable 5 — Scaling Triggers

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Companion script:** `scripts/capacity_check.sql` — one query per trigger below.
  Run it weekly. A threshold you cannot cheaply measure is a threshold you will not
  notice crossing.

---

## 0. The shape of it

You asked where the walls are. The honest summary is that **most of them are much
further away than you think, and the ones that are close are not the ones people
worry about.**

Two numbers, measured on this codebase rather than estimated:

```
score a 40-item attempt : 1.24 ms median  →  807 attempts/sec/core
publish gate, 39 items  : 3.77 ms median  →  265 gate runs/sec/core
```

At your MVP peak of 200 concurrent exam-takers, the scoring engine uses roughly
**0.03% of one core**. Nobody is going to re-architect because of scoring.

What will actually break, in the order it will happen:

| # | What | Roughly when | Warning it gives you |
|---|---|---|---|
| 1 | **Postgres connection exhaustion** | ~2× current load, or the day you add a second app process group | Bad. Random timeouts, no clear error |
| 2 | **Table bloat on `attempt_answers`** | 6–18 months of steady use | None. Autosaves just get slower |
| 3 | **Redis evicting a live competition payload** | Any time memory fills mid-contest | None until a student sees a blank exam |
| 4 | **Analytics queries competing with exams** | ~20–50 orgs | Gradual: exam latency creeps up during dashboard hours |
| 5 | **Media egress cost** | ~500 GB/month | Only the invoice |

Items 2 and 3 are the dangerous ones because they degrade silently. **I have
already fixed #2** — migration `0018` sets `fillfactor` and autovacuum thresholds
on the update-heavy tables (§5). #3 is a config change you should make when you
provision Redis (§6).

---

## 1. How to read this document

Each component gets: the **metric**, the **green band**, the **trigger**, what it
**feels like** when you cross it, and the **fix with its real cost**. Where a
threshold translates into users, I say so — but the metric is what you watch,
because user counts lie and metrics do not.

The baseline throughout is ADR-0001's MVP box: one VPS, 4 vCPU / 8 GB / NVMe,
Postgres + Redis + app + workers co-resident.

---

## 2. Web tier

| | |
|---|---|
| **Metric** | p95 request latency (non-media), and sustained CPU across gunicorn workers |
| **Green** | p95 < 200 ms, CPU < 40% |
| **Trigger** | **p95 > 400 ms sustained, or CPU > 60% for 15 minutes** |
| **In users** | ≈ 2,000 concurrent exam-takers, or ~15–20k DAU |

**Arithmetic.** A DB-backed JSON endpoint costs ~5 ms of Python CPU. Four cores at
70% utilisation ≈ 550 req/s. Your peak load is 200 concurrent × 1 autosave/10 s
= 20 writes/s plus reads, call it 50 req/s. **You are at roughly 10% of the box.**

**Fixes, in order:**

1. Double the VPS (4→8 vCPU). 30 minutes, +$17/month. Buys ~2×.
2. Move Postgres to its own VPS. 3–4 hours, +$17/month. Buys another ~1.5× and,
   more importantly, stops a heavy query from starving the web workers.
3. Second app node behind Caddy. 4–6 hours, +$17/month. Requires PgBouncer first
   (§3.1) and sticky-free sessions, which you already have — the access token is
   a JWT and the WebSocket ticket is single-use.

**The wall you cannot buy past:** vertical scaling tops out around 16 vCPU / 32 GB
on most providers (~$60–100/month). Step 3 is the answer, and nothing in the
architecture prevents it today.

---

## 3. PostgreSQL

Five distinct walls, and they arrive in a specific order.

### 3.1 Connections — the first thing that breaks

| | |
|---|---|
| **Metric** | `pg_stat_activity` count ÷ `max_connections` |
| **Green** | < 40% |
| **Trigger** | **> 60%** |

**Why this is first.** Nine gunicorn workers × a pool of 5 = 45 connections, plus
Dramatiq workers, plus the WS gateway, plus your `psql` session, against a default
`max_connections = 100`. You reach 60% at roughly twice your current process
count — which is exactly what step 1 or 3 above does.

**Why it is nasty.** It does not fail cleanly. You get sporadic connection
timeouts under load that look like network flakiness, and each idle Postgres
backend costs ~5–10 MB, so raising `max_connections` trades one problem for
memory pressure.

**Fix: PgBouncer in transaction mode.** 2–4 hours, $0. Do it *before* you add the
second app node, not after the incident. One caveat that matters here: transaction
pooling breaks session-level features — prepared statements, `SET LOCAL`,
advisory locks. You use none of those today, which is also why I did **not**
recommend row-level security in Deliverable 2 §8: RLS wants `SET LOCAL` per
transaction and would make this migration painful later.

### 3.2 Write throughput

| | |
|---|---|
| **Metric** | `pg_stat_database.xact_commit` rate; checkpoint warnings in the log |
| **Green** | < 200 writes/s |
| **Trigger** | **> 1,000 writes/s sustained, or "checkpoints occurring too frequently"** |
| **In users** | ≈ 20,000 concurrent exam-takers |

Not a real concern. A single NVMe box does 3,000–10,000 simple writes/s with
`synchronous_commit = on`. Listed so you can stop worrying about it.

### 3.3 Bloat on `attempt_answers` — the silent one

| | |
|---|---|
| **Metric** | `n_dead_tup / n_live_tup`, and the HOT-update ratio |
| **Green** | dead ratio < 0.05, HOT ratio > 0.90 |
| **Trigger** | **dead ratio > 0.20, or HOT ratio < 0.80** |

`attempt_answers` is the only genuinely UPDATE-heavy table in the system: every
autosave upserts a row, so one 40-question attempt produces ~200 row versions. Left
alone, the table bloats, the index follows, and autosaves get gradually slower with
no error anywhere.

**Already fixed, pre-emptively, in migration `0018`.** `fillfactor = 80` leaves
free space on each page so the updates stay HOT — the index is never touched and
dead tuples are reclaimed by page pruning rather than waiting for a vacuum. HOT
needs two conditions: page headroom, and no indexed column changing. `response`,
`revision` and `updated_at` are in no index, so fillfactor was the only missing
half. `attempts`, `outbox`, `media_uploads`, `notifications`, `idempotency_keys`
and `competition_results` got proportionate settings.

Section 4 of `capacity_check.sql` watches the HOT ratio. If it falls below 0.80,
lower fillfactor further (70) rather than reaching for anything cleverer.

### 3.4 Working set vs RAM

| | |
|---|---|
| **Metric** | `blks_hit / (blks_hit + blks_read)` |
| **Green** | > 0.99 |
| **Trigger** | **< 0.98** |
| **In users** | ≈ 50k users / ~30 GB of hot data on an 8 GB box |

Fix: more RAM first (vertical, cheap), then move analytics to a read replica
(§3.5). Do **not** start adding indexes in response to this — a falling cache hit
ratio means the working set outgrew memory, and more indexes make that worse.

### 3.5 Analytics competing with exams

| | |
|---|---|
| **Metric** | `pg_stat_statements`, ordered by `total_exec_time` |
| **Green** | no analytics query in the top 10 |
| **Trigger** | **an analytics query in the top 10, or `REFRESH MATERIALIZED VIEW` > 60 s** |
| **In users** | ≈ 20–50 organizations, or ~500k attempts |

This is the fourth thing that will bite you, and it presents as "the app feels slow
in the evenings" — which is when centre admins look at dashboards.

**Fix: a streaming read replica, and point `analytics` at it.** ~4 hours,
+$17/month. This is cheap *because* of a decision made in Deliverable 2: the
`analytics` module reads no other module's tables at runtime and is fed by domain
events, and an import-linter contract enforces that. Moving it is a connection
string, not a refactor.

### 3.6 Row growth

| | |
|---|---|
| **Metric** | rows per partition; total DB size vs disk |
| **Green** | < 20M rows per partition |
| **Trigger** | **any partition > 50M rows, or DB > 60% of disk** |

At 20k users (≈600k attempts/year) the volumes are:

| Table | Rows/year at 20k users | Handled by |
|---|---|---|
| `attempt_answers` | ~24 M | Plain table; fine to ~100 M |
| `item_exposures` | ~24 M | Monthly partitions, detached and archived |
| `attempt_answer_events` | ~120 M | Monthly partitions + **90-day retention** |
| `audit_log` | ~5 M | Monthly partitions, retained indefinitely |

`attempt_answer_events` is the one to watch — it is the highest-volume table and
the least valuable per row. If retention becomes the binding constraint, cut it to
30 days before you consider anything more elaborate; its only consumers are
anti-cheat heuristics and dispute forensics, both of which are recent-window
questions.

**Watch for rows landing in a `_default` partition** (§7 of the capacity script).
That means the monthly partition job did not run. The default partitions exist so
this degrades into "rows in the wrong place, recoverable" rather than "inserts
failing mid-exam", but it still needs fixing within the month.

---

## 4. Redis

| | |
|---|---|
| **Metric** | `used_memory` ÷ `maxmemory`; `evicted_keys` |
| **Green** | < 50%, zero evictions |
| **Trigger** | **> 70%, or `evicted_keys > 0` — ever** |

**Any eviction is an incident, not a warning.** A competition payload evicted
during the lobby means a student's exam does not load at T-0, and there is no
recovery inside the contest window.

**Configure this correctly at provisioning time:**

* Competition payloads and prefetch blobs go in their own logical DB with
  `maxmemory-policy noeviction`. Better a write error you see than a silent
  eviction you do not.
* Rate-limit counters, presence and leaderboards go in a second logical DB with
  `volatile-ttl`. All of it is reconstructible.
* Alert on `evicted_keys > 0` from minute one.

Sizing: a 200 KB snapshot × 50 concurrent competitions ≈ 10 MB. Leaderboards,
presence and rate limits are noise by comparison. 256 MB is generous; the risk is
misconfiguration, not volume.

Nothing durable lives in Redis — the job queue is fed by the Postgres outbox — so
a total Redis loss costs you cached state and in-flight competitions, not data.

---

## 5. Background workers

| | |
|---|---|
| **Metric** | **outbox lag**: age of the oldest undispatched row |
| **Green** | < 5 s |
| **Trigger** | **> 60 s sustained, or any row with `attempts > 3`** |

Outbox lag is the single best worker-health signal in the system, it is one SQL
query, and it covers every asynchronous path at once — regrade, notifications,
analytics projections, webhook retries. Watch this one number.

**The CPU hog is audio transcode**: a 30-minute WAV costs 30–60 s of ffmpeg CPU,
and it will happily starve the web workers on a shared box.

| Situation | Fix | Cost |
|---|---|---|
| Occasional transcode contention | `nice` the transcode worker; cap concurrency at 1 | 15 min |
| > 20 audio uploads/day overlapping exam hours | Dedicated worker VPS | 2 h, +$8/mo |
| Regrade of > 50k attempts | Batch it; 1.24 ms/attempt means 50k = ~60 s of CPU | — |

That last row is worth stating plainly: **a full regrade of every attempt you will
have in year one takes about a minute of CPU.** Regrade is not a scaling problem.

---

## 6. WebSocket gateway

| | |
|---|---|
| **Metric** | concurrent sockets; broadcast messages/sec; event-loop lag |
| **Green** | < 1,000 sockets |
| **Trigger** | **> 5,000 concurrent sockets, or > 5,000 broadcast msg/s, or loop lag > 100 ms** |

An idle socket costs ~10–30 KB. 200 concurrent is 6 MB; 10,000 is 300 MB. Memory
is not the wall — **broadcast fan-out is**. A leaderboard delta to 5,000
subscribers every 3 s is ~1,700 msg/s, which one async process handles. At 50,000
subscribers it is 17k msg/s and you shard by channel across processes.

Also watch **file descriptors** against `ulimit -n`; the default 1024 will stop you
at ~900 sockets long before anything else does. Raise it at provisioning.

---

## 7. Media and TURN

### Media egress

| | |
|---|---|
| **Metric** | monthly egress from object storage + origin |
| **Green** | < 100 GB/month |
| **Trigger** | **> 500 GB/month** |
| **In users** | ≈ 5,000 DAU |

Per-user tokenized URLs mean **zero CDN cache hits by construction** — that is the
anti-scrape design working as intended, and it is the right trade at ~20 GB/month.
At 500 GB the arithmetic flips.

Fix: signed CDN URLs with a coarser grant — per attempt-section rather than per
request — accepting weaker per-request attribution while keeping exposure logging
at the section level. ~1 day.

### TURN

| | |
|---|---|
| **Metric** | monthly relay egress; **relay rate** from `speaking_pairs.turn_relayed` |
| **Green** | < 300 GB/month, relay rate < 30% |
| **Trigger** | **> 1 TB/month, or relay rate > 40%** |

The relay *rate* is the more informative number and almost nobody tracks it. The
ADR's cost estimate assumes 30% of pairs need a relay; if that climbs to 60%
because a carrier tightened its NAT, your bandwidth doubles with no change in
usage. You are recording it on every session (`POST /speaking/pairs/{xid}/end`)
precisely so this is observable.

coturn handles roughly 1,000 concurrent relays per core, so CPU is not the wall
before bandwidth is.

---

## 8. Compute paths, measured

| Path | Measured | Trigger | Note |
|---|---|---|---|
| Score a 40-item attempt | **1.24 ms** (807/s/core) | > 20 ms | Not a scaling concern at any size you will reach |
| Publish gate, 39 questions | **3.77 ms** (265/s/core) | > 2 s | An author runs it once per publish |
| Full regrade, 50k attempts | ~60 s CPU | > 10 min | Batch and report progress |

I looked at memoizing the compiled JSON Schema validators in the publish gate —
they are currently rebuilt per question. At 3.77 ms for a whole test it is not
worth the cache-invalidation surface. Revisit only if the gate exceeds 2 s, which
would take a test of roughly 400 questions.

---

## 9. Deliberately not on this list

Things people expect to see, that are not walls for you:

| Not a concern | Why |
|---|---|
| Scoring throughput | 0.03% of one core at peak load |
| Postgres full-text search | `pg_trgm` over a few thousand tests is sub-millisecond. Elasticsearch stays off the table until ~500k documents |
| JSONB query performance | Payloads are read by primary key, never searched inside |
| The 200-concurrent competition start | Solved by design (two-phase prefetch); the herd is a 100-byte request |
| Number of question types | Registry lookups are a dict. 17 or 1,700 costs the same |
| WebSocket memory | 300 MB at 10,000 sockets |

---

## 10. The wall that is not technical

The binding constraint on this system is not any number above. It is that **one
person maintains all of it.**

| Metric | Trigger | What it means |
|---|---|---|
| Ops toil | **> 4 hours/week sustained** | You are now maintaining infrastructure instead of building the authoring system, which is the product |
| Incident frequency | **> 1 user-visible incident/month** | Buy managed Postgres before you buy anything else — it is the highest-toil component |
| B2B clients | **> 5 paying centres** | Someone will ask for an SLA, a data-processing agreement, and an in-country hosting attestation. §9.1 of the ADR becomes urgent rather than prudent |
| Team | **a second engineer** | RLS (D2 §8), a staging environment separate from production, and proper CI/CD all start earning their keep |

If you cross the ops-toil line, the correct response is to **spend money to buy
hours back** — managed Postgres at $30–50/month is cheap against four hours a week
of your time — even though it partly conflicts with the portability argument in
ADR-0001 §9.1. Note the tension honestly: managed Postgres from a non-Uzbek
provider is exactly the lock-in the residency requirement warns against. If you
reach that point *after* signing B2B contracts, the answer is a managed Uzbek
provider or a paid DBA day, not a European cloud.

---

## 11. What to instrument on day one

Everything in this document is measurable with what you already have. The full
cost is one cron job.

```
weekly   psql "$DATABASE_URL" -f scripts/capacity_check.sql   → email to yourself
always   Sentry (errors) · UptimeRobot (availability) · Redis evicted_keys alert
monthly  object storage egress · SMS spend from notifications.cost_minor
quarterly restore drill, timed and written down (ADR-0001 §10)
```

Add Prometheus and Grafana when you have a second engineer, not before. At one
person, a weekly email you actually read beats a dashboard you do not.

---

## 12. Summary: the first five moves, in order

| When | Move | Hours | $/month |
|---|---|---|---|
| Now | `fillfactor` + autovacuum tuning | **done** (migration 0018) | 0 |
| Now | Redis: `noeviction` for competition payloads, alert on evictions | 0.5 | 0 |
| Now | Raise `ulimit -n` on the app process | 0.1 | 0 |
| Connections > 60% | PgBouncer, transaction mode | 2–4 | 0 |
| CPU > 60% | Double the VPS | 0.5 | +17 |
| Analytics in top-10 slow queries | Read replica for `analytics` | 4 | +17 |
| CPU > 60% again | Split Postgres onto its own box | 3–4 | +17 |
| Still > 60% | Second app node behind Caddy | 4–6 | +17 |

**Total to go from your current box to roughly 20–50k users: ~15 hours of work and
about $70/month.** No rewrite, no new technology, and no decision in this design
that has to be undone to get there. That was the claim in ADR-0001 §0, and this
document is the arithmetic behind it.

---

## 13. Where I could be wrong

1. **The 5 ms per-request estimate is not measured.** Scoring and the publish gate
   are; the full request path is not, because there is no HTTP layer yet. Once
   phase 5 lands, replace it with a real p95 and expect it to be worse than 5 ms —
   ORM hydration is usually the surprise.

2. **`attempt_answer_events` at ~120 M rows/year may prove not worth its keep.** It
   exists for anti-cheat and dispute forensics. If neither uses it in the first
   year, drop the table rather than tuning it.

3. **The relay-rate assumption (30%) is a guess.** It is the single largest error
   bar in the cost model, which is why it is instrumented from day one. If it
   comes back at 60%, TURN roughly doubles to $10–20/month — still small, but the
   estimate deserves a real number rather than my assumption.

4. **The read-replica trigger may be too late.** "Analytics in the top 10" means
   users are already feeling it. If you would rather move earlier, the alternative
   trigger is "any B2B dashboard query > 2 s at p95", which fires sooner and costs
   you $17/month earlier.

---

## Deliverable 5 ends here — and with it, all five.

Remaining build work is phases 5–8 of ADR-0001 §12: repositories, the HTTP layer
against the Deliverable 3 contract, the import pipeline, and the exam session
lifecycle. Say the word and I will continue in the same phase order.
