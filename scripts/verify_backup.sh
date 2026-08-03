#!/bin/sh
# Restore the newest backup into a scratch database and ask whether it is real.
#
# **This is the part that makes the backup a backup.** An untested dump is a
# hypothesis, and the moment you discover it was wrong is the moment you needed
# it. Running this on a schedule turns "we have backups" into a claim with
# evidence behind it.
#
# It restores into a THROWAWAY database beside the live one, never over it, so
# it is safe to run on the production box at three in the morning. The cost is
# one restore's worth of disk and CPU; the alternative is finding out during an
# incident.
#
# What it asserts, and why these:
#
#   * the checksums match          — a truncated copy restores partially and
#                                    silently, and pg_restore exits 0 on some of
#                                    them
#   * the restore itself succeeds  — the obvious one
#   * the restored row counts MATCH THE LIVE ONES — not "are non-empty", which
#                                    was the first attempt and is wrong: a
#                                    brand-new production database legitimately
#                                    has no attempts until the first student
#                                    sits a mock, so that check would have
#                                    failed every night of the first week. A
#                                    verify that cries wolf is a verify nobody
#                                    reads. Comparing against live is the
#                                    property that was actually wanted, and it
#                                    holds on day one and on day one hundred
#   * every attempt's test_version still resolves — the dump is internally
#                                    consistent rather than a mid-write snapshot
#   * media_assets rows have files in the archive — the cross-store check, and
#                                    the one nothing else in this system does
set -eu

: "${PGHOST:=postgres}"
: "${PGUSER:=ielts}"
: "${PGDATABASE:=ielts}"
: "${BACKUP_DIR:=/backups}"
: "${VERIFY_DATABASE:=ielts_verify}"

log() { echo "[verify] $*" >&2; }
fail() { log "FAILED: $1"; exit 1; }

latest="${1:-$(ls -1d "${BACKUP_DIR}"/2*Z 2>/dev/null | sort | tail -n 1)}"
[ -n "$latest" ] && [ -d "$latest" ] || fail "no backup found in ${BACKUP_DIR}"
log "verifying $(basename "$latest")"

# ── the checksums, before anything else ──────────────────────────────────────
( cd "$latest" && sha256sum -c SHA256SUMS >/dev/null 2>&1 ) \
    || fail "checksums do not match — this backup is corrupt"

# ── restore into a scratch database ──────────────────────────────────────────
psql -v ON_ERROR_STOP=1 -d postgres \
     -c "DROP DATABASE IF EXISTS ${VERIFY_DATABASE}" >/dev/null \
    || fail "could not drop the scratch database"
psql -v ON_ERROR_STOP=1 -d postgres \
     -c "CREATE DATABASE ${VERIFY_DATABASE}" >/dev/null \
    || fail "could not create the scratch database"

# `--no-owner` because the scratch database is created by whoever runs this and
# the dump's owner may not exist here. Errors are collected rather than fatal on
# the first one: pg_restore reports several for a genuinely broken dump and the
# first is rarely the informative one.
if ! pg_restore --no-owner --no-privileges -d "${VERIFY_DATABASE}" \
        "${latest}/database.dump" 2>"${latest}/restore.log"; then
    log "pg_restore reported errors:"
    tail -n 20 "${latest}/restore.log" >&2
    fail "restore"
fi

ask() {
    psql -tAX -d "${VERIFY_DATABASE}" -c "$1"
}

# ── does it hold what the live database holds ────────────────────────────────
#
# `restored <= live` is expected and fine: rows arrive between the dump and this
# comparison. The two failures worth catching are the other directions —
# restored > live means the dump is not of this database, and live > 0 with
# restored = 0 means it captured nothing at all, which is the "backup worked,
# restored an empty schema" case.
for table in users attempts score_runs question_versions media_assets; do
    restored="$(ask "SELECT count(*) FROM ${table}")" || fail "querying ${table}"
    live="$(psql -tAX -d "${PGDATABASE}" -c "SELECT count(*) FROM ${table}")" \
        || fail "querying live ${table}"
    log "  ${table}: ${restored} restored, ${live} live"
    [ "$restored" -le "$live" ] \
        || fail "${table}: ${restored} restored but only ${live} live — this dump is not of this database"
    if [ "$live" -gt 0 ] && [ "$restored" -eq 0 ]; then
        fail "${table}: the live database has ${live} rows and the dump restored none"
    fi
done

# ── is it internally consistent ──────────────────────────────────────────────
#
# A dump taken while something was half-written would show up here. pg_dump is
# transactional so this should never fire; it is here because "should never"
# is not a property anyone should take on trust about their only backup.
orphans="$(ask "
    SELECT count(*) FROM attempts a
    LEFT JOIN test_versions v ON v.id = a.test_version_id
    WHERE v.id IS NULL")" || fail "consistency query"
[ "$orphans" = "0" ] || fail "${orphans} attempts reference a test version that is not in this dump"

# ── does the media match the database ────────────────────────────────────────
#
# The cross-store check, and the reason `backup.sh` dumps before it archives. A
# database restored without its media has attempts pointing at audio that does
# not exist, and nothing else in this system would notice.
missing=0
checked=0
for key in $(ask "
    SELECT storage_key FROM media_assets
    WHERE status = 'ready' ORDER BY id DESC LIMIT 25"); do
    checked=$((checked + 1))
    tar -tf "${latest}/media.tar" "./${key}" >/dev/null 2>&1 || {
        log "  missing from the archive: ${key}"
        missing=$((missing + 1))
    }
done
log "  media: ${checked} recent assets checked, ${missing} missing"
[ "$missing" = "0" ] || fail "the archive is missing files the database references"

psql -v ON_ERROR_STOP=1 -d postgres \
     -c "DROP DATABASE ${VERIFY_DATABASE}" >/dev/null || true

log "OK: $(basename "$latest") restores, and its media matches its database"
