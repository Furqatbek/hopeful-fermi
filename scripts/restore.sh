#!/bin/sh
# Put a backup back. This one destroys data, so it asks first.
#
# Read `docs/deploy/production.md` before running it in anger — the order below
# matters and there is a step this script deliberately does NOT do for you.
#
# **Stop the application first.** This script refuses to guess: the api, worker
# and scheduler hold connections and will write into a half-restored database if
# they are up. `docker compose stop api worker scheduler` and then run this.
#
# The order here is the mirror of `backup.sh` and for the same reason: media
# goes back FIRST, so by the time the database is live every file it references
# already exists. Restoring the database first leaves a window — usually
# seconds, but a student can start an exam in seconds — where a row names a file
# that is not on disk yet.
set -eu

: "${PGHOST:=postgres}"
: "${PGUSER:=ielts}"
: "${PGDATABASE:=ielts}"
: "${BACKUP_DIR:=/backups}"
: "${MEDIA_DIR:=/var/lib/ielts/media}"

log() { echo "[restore] $*" >&2; }
fail() { log "FAILED: $1"; exit 1; }

stamp="${1:-}"
if [ -z "$stamp" ]; then
    echo "usage: restore.sh <stamp|latest>" >&2
    echo "" >&2
    echo "available:" >&2
    ls -1d "${BACKUP_DIR}"/2*Z 2>/dev/null | sort -r | head -n 20 >&2
    exit 2
fi
if [ "$stamp" = "latest" ]; then
    source_dir="$(ls -1d "${BACKUP_DIR}"/2*Z 2>/dev/null | sort | tail -n 1)"
else
    source_dir="${BACKUP_DIR}/${stamp}"
fi
[ -n "$source_dir" ] && [ -d "$source_dir" ] || fail "no such backup: ${stamp}"

log "restoring from $(basename "$source_dir")"
cat "${source_dir}/manifest.txt" >&2

# Checksums before anything is touched. A truncated transfer that is discovered
# halfway through a restore has already destroyed the thing it was replacing.
( cd "$source_dir" && sha256sum -c SHA256SUMS >/dev/null 2>&1 ) \
    || fail "checksums do not match — refusing to restore a corrupt backup"

if [ "${RESTORE_CONFIRM:-}" != "yes-destroy-the-current-data" ]; then
    cat >&2 <<EOF

This DROPS the ${PGDATABASE} database and REPLACES ${MEDIA_DIR}.
Everything since $(basename "$source_dir") is lost.

Stop the application first:   docker compose stop api worker scheduler
Then re-run with:             RESTORE_CONFIRM=yes-destroy-the-current-data

EOF
    exit 3
fi

# ── 1. media first, so no row ever names a file that is not there yet ────────
log "restoring media"
mkdir -p "${MEDIA_DIR}"
# Into a sibling and then swapped, rather than extracted over the top: a failed
# extraction halfway through would otherwise leave a directory that is neither
# the old set nor the new one.
staging="${MEDIA_DIR}.restoring"
rm -rf "$staging"
mkdir -p "$staging"
tar -xf "${source_dir}/media.tar" -C "$staging" || fail "extracting media"
rm -rf "${MEDIA_DIR}.previous"
mv "${MEDIA_DIR}" "${MEDIA_DIR}.previous" 2>/dev/null || true
mv "$staging" "${MEDIA_DIR}" || fail "swapping media into place"
log "previous media kept at ${MEDIA_DIR}.previous — delete it once you are happy"

# ── 2. the database ──────────────────────────────────────────────────────────
log "dropping and recreating ${PGDATABASE}"
psql -v ON_ERROR_STOP=1 -d postgres \
     -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
         WHERE datname = '${PGDATABASE}' AND pid <> pg_backend_pid()" >/dev/null \
    || fail "could not disconnect existing sessions"
psql -v ON_ERROR_STOP=1 -d postgres -c "DROP DATABASE IF EXISTS ${PGDATABASE}" \
    >/dev/null || fail "drop"
psql -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE ${PGDATABASE}" \
    >/dev/null || fail "create"

log "restoring the database"
pg_restore --no-owner --no-privileges -d "${PGDATABASE}" \
    "${source_dir}/database.dump" || fail "pg_restore"

log "done."
cat >&2 <<EOF

Next, in this order:

  1. docker compose up -d migrate     # brings the schema to head if the backup
                                      # predates a migration. Idempotent.
  2. docker compose up -d             # api, worker, scheduler
  3. Sit one mock end to end, with audio, before telling anyone it is back.
     A database that restored cleanly and a paper that plays are different
     claims, and only the second one is the product.

EOF
