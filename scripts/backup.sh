#!/bin/sh
# One backup: the database, then the media, then a manifest tying them together.
#
# **The order is load-bearing and it is the only subtle thing in this file.**
#
# `pgdata` and `media` are two stores that reference each other: an
# `media_assets` row names a file, and a file is worthless without the row.
# Whichever is captured second must be the SUPERSET, so:
#
#   dump first, then archive media
#     -> media contains everything the dump references, plus anything uploaded
#        in between. Those extras are orphans: files nothing points at, which
#        cost disk and break nothing.
#
#   media first, then dump
#     -> the dump can reference a file uploaded after the archive was taken.
#        That is a listening section a student opens to a 404 in a timed exam,
#        and nothing in the restore would report it.
#
# So: database, then media. Never the other way round, and never in parallel.
#
# Everything here is POSIX sh and the tools in the `postgres` image, because
# this runs in a sidecar built from that image — `pg_dump` must match the server
# major version, and the surest way to guarantee that is to use the same image.
set -eu

: "${PGHOST:=postgres}"
: "${PGUSER:=ielts}"
: "${PGDATABASE:=ielts}"
: "${BACKUP_DIR:=/backups}"
: "${MEDIA_DIR:=/var/lib/ielts/media}"
: "${BACKUP_KEEP_DAILY:=7}"
: "${BACKUP_KEEP_WEEKLY:=4}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${BACKUP_DIR}/${stamp}"
mkdir -p "$target"

log() { echo "[backup] $*" >&2; }

# A partial backup that looks complete is worse than an obvious failure: the
# next restore drill would pass and the real restore would not. Anything that
# dies leaves the directory renamed, so it can never be mistaken for the newest
# good one by `restore.sh`, which sorts on the name.
fail() {
    log "FAILED: $1"
    mv "$target" "${target}.failed" 2>/dev/null || true
    exit 1
}

# ── 1. the database ──────────────────────────────────────────────────────────
#
# `-Fc` is the custom format: compressed, and restorable table-by-table with
# `pg_restore`, which a plain SQL file is not. That matters during an incident,
# when the thing you want is usually one table rather than the whole cluster.
log "dumping ${PGDATABASE} from ${PGHOST}"
pg_dump -Fc -Z6 --no-owner --no-privileges -f "${target}/database.dump" \
    || fail "pg_dump"

# ── 2. the media, second, for the reason at the top ──────────────────────────
#
# Uncompressed: this is m4a, wav and jpeg, all already compressed, and gzip
# spends CPU on a box that shares four cores with live exams to save nothing.
log "archiving media from ${MEDIA_DIR}"
tar -cf "${target}/media.tar" -C "${MEDIA_DIR}" . || fail "tar media"

# ── 3. the manifest ──────────────────────────────────────────────────────────
#
# Checksums, because a backup nobody has verified is a hypothesis. `restore.sh`
# checks these before it touches anything, so a truncated transfer is caught
# before it has overwritten a working database rather than after.
log "writing manifest"
(
    cd "$target"
    sha256sum database.dump media.tar > SHA256SUMS
) || fail "checksums"

cat > "${target}/manifest.txt" <<EOF
taken_at        ${stamp}
database        ${PGDATABASE} on ${PGHOST}
database_bytes  $(wc -c < "${target}/database.dump")
media_bytes     $(wc -c < "${target}/media.tar")
order           database first, then media — media is the superset, see backup.sh
restore_with    scripts/restore.sh ${stamp}
EOF

# ── 4. retention ─────────────────────────────────────────────────────────────
#
# Daily for a week, then one a week for a month. Deliberately not "keep
# everything": this disk also holds the database and every uploaded file, and a
# backup directory that grows without a ceiling takes down uploads and exams
# together — the same failure mode as a full media directory.
#
# Weeklies are chosen as the FIRST backup of each ISO week still present, so the
# set thins out rather than developing holes.
log "pruning"
cd "$BACKUP_DIR"
all="$(ls -1d 2*Z 2>/dev/null | sort -r || true)"
keep="$(echo "$all" | head -n "$BACKUP_KEEP_DAILY")"
weeks=""
for dir in $all; do
    week="$(date -u -d "$(echo "$dir" | sed 's/T.*//')" +%G%V 2>/dev/null || echo "")"
    [ -z "$week" ] && continue
    case " $weeks " in *" $week "*) continue ;; esac
    weeks="$weeks $week"
    keep="$keep
$dir"
    [ "$(echo "$weeks" | wc -w)" -ge "$BACKUP_KEEP_WEEKLY" ] && break
done
for dir in $all; do
    case "$(echo "$keep" | tr '\n' ' ')" in
        *"$dir"*) ;;
        *) log "pruning $dir"; rm -rf "$dir" ;;
    esac
done

log "done: ${target} ($(du -sh "$target" | cut -f1))"

# ── 5. what this does NOT do ─────────────────────────────────────────────────
#
# It does not copy anything off this box, and that is the operator's step
# because the destination is a residency decision, not a technical one: §9.1
# says personal data of Uzbek citizens, including minors, may be required to
# stay in-country, so a default of "some S3 bucket in Frankfurt" would be a
# legal problem shipped as a convenience.
#
# A backup that never leaves the disk it protects is not a backup — it survives
# `DROP TABLE` and a bad migration, and not a failed NVMe or a lost box. Copy
# ${BACKUP_DIR} to a second machine in-country, encrypted in transit, and put
# that in a cron on the RECEIVING side so a compromised app box cannot delete
# its own history. docs/deploy/production.md has the shape of it.
