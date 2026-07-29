-- Capacity check.
--
-- One query per scaling trigger in docs/design/0005-scaling-triggers.md. Run it
-- weekly (a cron that emails the output is enough at MVP) and act on anything
-- flagged. A threshold you cannot cheaply measure is a threshold you will not
-- notice crossing.
--
--   psql "$DATABASE_URL" -f scripts/capacity_check.sql
--
-- Requires pg_stat_statements for section 8; everything else is core.

\pset border 2
\timing off

\echo '=== 1. CONNECTIONS  (trigger: in_use > 60% of max_connections -> PgBouncer)'
SELECT (SELECT count(*) FROM pg_stat_activity)                       AS in_use,
       current_setting('max_connections')::int                       AS max_connections,
       round(100.0 * (SELECT count(*) FROM pg_stat_activity)
             / current_setting('max_connections')::int, 1)           AS pct,
       CASE WHEN (SELECT count(*) FROM pg_stat_activity)
                 > 0.6 * current_setting('max_connections')::int
            THEN 'ACT: add PgBouncer (transaction mode)' ELSE 'ok' END AS verdict;

\echo ''
\echo '=== 2. CACHE HIT RATIO  (trigger: < 0.98 -> more RAM, then a read replica)'
SELECT round(sum(blks_hit)::numeric / NULLIF(sum(blks_hit + blks_read), 0), 4) AS hit_ratio,
       pg_size_pretty(pg_database_size(current_database()))                    AS db_size,
       current_setting('shared_buffers')                                       AS shared_buffers,
       CASE WHEN sum(blks_hit)::numeric / NULLIF(sum(blks_hit + blks_read), 0) < 0.98
            THEN 'ACT: working set no longer fits in RAM' ELSE 'ok' END        AS verdict
FROM pg_stat_database WHERE datname = current_database();

\echo ''
\echo '=== 3. TABLE BLOAT  (trigger: dead/live > 0.20 on an update-heavy table)'
SELECT relname,
       n_live_tup, n_dead_tup,
       round(n_dead_tup::numeric / NULLIF(n_live_tup, 0), 3) AS dead_ratio,
       last_autovacuum,
       CASE WHEN n_dead_tup::numeric / NULLIF(n_live_tup, 0) > 0.20
            THEN 'ACT: autovacuum is not keeping up' ELSE 'ok' END AS verdict
FROM pg_stat_user_tables
WHERE n_live_tup > 1000
ORDER BY n_dead_tup::numeric / NULLIF(n_live_tup, 0) DESC NULLS LAST
LIMIT 10;

\echo ''
\echo '=== 4. HOT UPDATE RATIO  (trigger: < 0.80 on attempt_answers -> raise fillfactor)'
SELECT relname, n_tup_upd, n_tup_hot_upd,
       round(n_tup_hot_upd::numeric / NULLIF(n_tup_upd, 0), 3) AS hot_ratio,
       CASE WHEN n_tup_upd > 10000
                 AND n_tup_hot_upd::numeric / NULLIF(n_tup_upd, 0) < 0.80
            THEN 'ACT: updates are leaving the page; lower fillfactor further'
            ELSE 'ok' END AS verdict
FROM pg_stat_user_tables
WHERE relname IN ('attempt_answers', 'attempts', 'outbox', 'media_uploads', 'notifications')
ORDER BY n_tup_upd DESC;

\echo ''
\echo '=== 5. LARGEST RELATIONS  (trigger: any partition > 50M rows, or db > 60% of disk)'
SELECT c.relname,
       to_char(c.reltuples::bigint, 'FM999,999,999')        AS est_rows,
       pg_size_pretty(pg_total_relation_size(c.oid))        AS total_size,
       CASE WHEN c.reltuples > 50e6
            THEN 'ACT: shorten retention or detach older partitions'
            ELSE 'ok' END                                   AS verdict
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 10;

\echo ''
\echo '=== 6. OUTBOX LAG  (trigger: oldest undispatched > 60s -> workers are behind)'
SELECT count(*)                                                     AS pending,
       coalesce(round(extract(epoch FROM now() - min(available_at))), 0) AS oldest_seconds,
       count(*) FILTER (WHERE attempts > 3)                         AS stuck,
       CASE WHEN coalesce(extract(epoch FROM now() - min(available_at)), 0) > 60
            THEN 'ACT: add worker processes, or a worker box'
            ELSE 'ok' END                                           AS verdict
FROM outbox WHERE dispatched_at IS NULL;

\echo ''
\echo '=== 7. PARTITION COVERAGE  (trigger: rows landing in a DEFAULT partition)'
SELECT c.relname AS default_partition,
       c.reltuples::bigint AS est_rows,
       CASE WHEN c.reltuples > 0
            THEN 'ACT: the monthly partition job did not run'
            ELSE 'ok' END AS verdict
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname LIKE '%\_default';

\echo ''
\echo '=== 8. SLOWEST STATEMENTS  (trigger: analytics in the top 10 -> read replica)'
\echo '(requires pg_stat_statements; skipped silently if absent)'
SELECT round(total_exec_time)::bigint AS total_ms,
       calls,
       round(mean_exec_time, 1)       AS mean_ms,
       left(regexp_replace(query, '\s+', ' ', 'g'), 90) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;

\echo ''
\echo '=== 9. LIVE EXAM LOAD  (context for everything above)'
SELECT count(*) FILTER (WHERE status = 'in_progress')                    AS attempts_live,
       count(*) FILTER (WHERE status = 'in_progress'
                        AND expires_at < now() + interval '5 minutes')   AS expiring_soon,
       count(*) FILTER (WHERE created_at > now() - interval '24 hours')  AS attempts_24h
FROM attempts;

\echo ''
\echo '=== 10. CONTENT SCALE  (context: when analytics and the library start to hurt)'
SELECT (SELECT count(*) FROM organizations WHERE status = 'active') AS orgs,
       (SELECT count(*) FROM users WHERE deleted_at IS NULL)        AS users,
       (SELECT count(*) FROM test_versions WHERE status = 'published') AS published_versions,
       (SELECT count(*) FROM questions WHERE archived_at IS NULL)   AS questions;
