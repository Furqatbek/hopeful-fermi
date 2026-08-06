#!/usr/bin/env bash
# One command from a fresh clone to a signed-in developer.
#
#     make dev
#
# Starts PostgreSQL and Redis, builds the virtualenv, migrates, creates a first
# account, and runs the API with reload. Nothing needs to exist first: no .env,
# no exported variables, no psql.
#
# **Every step is skipped when it is already done**, so this is also the command
# you run every morning. It is idempotent, not clever: each step asks the system
# a question and acts on the answer, rather than keeping state in a file that
# can disagree with reality.
#
# ## Why a first account is created here
#
# A fresh database has no users, and you cannot register through the API:
# registration goes through `POST /auth/telegram/verify` and needs a payload
# signed with a real bot token. That is deliberate — the version that accepted a
# bare phone number was a complete authentication bypass — but it means the
# documented local setup used to stop dead at the sign-in screen with a psql
# command to run by hand. A step everyone must perform and nobody can guess is a
# step that belongs in the script.
#
# The seed refuses to run unless ENVIRONMENT is `development` and the users
# table is empty. Both conditions, because either alone is one accident away
# from creating a platform admin on somebody's staging box.
#
# ## What it does NOT do
#
# It does not start the console — `make dev-web` does, in its own terminal.
# Backgrounding a second long-lived process from a Makefile and cleaning it up
# reliably on every exit path is more machinery than one saved terminal is
# worth, and the failure mode is an orphaned vite holding port 5173 after the
# API is gone.
#
# It does not start MinIO or ffmpeg. The file storage backend is the default and
# writes to ./var/media; audio TRANSCODE needs ffmpeg and everything else does
# not, so requiring it here would be charging every developer for a feature most
# sessions never touch.

set -euo pipefail
cd "$(dirname "$0")/.."

BLUE=$'\033[34m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { printf '%s==>%s %s\n' "$BLUE" "$OFF" "$1"; }
note() { printf '%s    %s%s\n' "$DIM" "$1" "$OFF"; }

# ── configuration, all of it defaulted ───────────────────────────────
#
# `:=` throughout, so every one of these is an override and none is a
# requirement. Exported because alembic and uvicorn are separate processes.
: "${ENVIRONMENT:=development}"
: "${DATABASE_URL:=postgresql+psycopg://postgres@127.0.0.1:55432/ielts}"
: "${REDIS_URL:=redis://127.0.0.1:6399/0}"
# The dev sign-in shortcut: `POST /auth/otp/request` returns the code in its own
# response, because there is no SMS provider. It is account takeover by design
# and `config.py` logs a warning at every boot. Fine on a laptop; anywhere else
# it is the authentication system switched off.
: "${PILOT_OPEN_SIGNIN:=true}"
: "${API_PORT:=8000}"
: "${DEV_PHONE:=+998901234567}"
export ENVIRONMENT DATABASE_URL REDIS_URL PILOT_OPEN_SIGNIN

if [ "$ENVIRONMENT" != "development" ]; then
    echo "refusing: ENVIRONMENT is '$ENVIRONMENT'. This script seeds an account" >&2
    echo "and turns on open sign-in. It is for laptops only." >&2
    exit 1
fi

# ── the virtualenv ───────────────────────────────────────────────────
if [ -z "${VIRTUAL_ENV:-}" ] && [ ! -x .venv/bin/python ]; then
    step "Creating .venv"
    PY=""
    for candidate in python3.13 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
            'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)'; then
            PY="$candidate"; break
        fi
    done
    [ -n "$PY" ] || { echo "need Python 3.12+; found none on PATH" >&2; exit 1; }
    "$PY" -m venv .venv
fi
if [ -z "${VIRTUAL_ENV:-}" ]; then
    # shellcheck disable=SC1091
    . .venv/bin/activate
fi

if ! python -c 'import app' >/dev/null 2>&1; then
    step "Installing dependencies"
    note "first run only; a minute or two"
    pip install --quiet --upgrade pip
    pip install --quiet -e '.[dev]'
fi

# ── services ─────────────────────────────────────────────────────────
db_ready() { python - <<'PY' >/dev/null 2>&1
import os
from sqlalchemy import create_engine, text
create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True) \
    .connect().execute(text("select 1"))
PY
}

if db_ready; then
    step "PostgreSQL already reachable"
    note "$DATABASE_URL"
else
    command -v docker >/dev/null 2>&1 || {
        echo "PostgreSQL is not reachable at DATABASE_URL and docker is not" >&2
        echo "installed. Start PostgreSQL 16 and Redis yourself, or set" >&2
        echo "DATABASE_URL and REDIS_URL to where they already are." >&2
        exit 1; }
    step "Starting PostgreSQL and Redis"
    docker compose -f docker-compose.dev.yml up -d --wait
    for _ in $(seq 1 60); do db_ready && break; sleep 1; done
    db_ready || { echo "the database did not come up; try 'make dev-reset'" >&2; exit 1; }
fi

# ── schema ───────────────────────────────────────────────────────────
step "Applying migrations"
alembic upgrade head 2>&1 | grep -E "Running upgrade|already at" || true

# ── a first account ──────────────────────────────────────────────────
step "Checking for an account"
python - "$DEV_PHONE" <<'PY'
import os
import sys

from sqlalchemy import create_engine, text

phone = sys.argv[1]
engine = create_engine(os.environ["DATABASE_URL"])
with engine.begin() as c:
    # Both guards, and neither is redundant: the environment check keeps this
    # off a staging box, and the emptiness check keeps a re-run from quietly
    # granting platform admin on a database that already has real people in it.
    if c.scalar(text("SELECT count(*) FROM users")):
        print(f"    an account already exists; signing in as {phone} still works")
        sys.exit(0)
    uid = c.scalar(text("""
        INSERT INTO users (phone, given_name, date_of_birth, locale, status)
        VALUES (:p, 'Aziza', '2000-01-01', 'uz-Latn', 'active') RETURNING id
    """), {"p": phone})
    # `granted_by` is itself. That is what a bootstrap looks like, and it is the
    # same shape as the production one: there is no endpoint that mints a
    # platform admin, deliberately, because an API that can is a much larger
    # blast radius than a step somebody performs once.
    c.execute(text("""INSERT INTO platform_role_grants (user_id, role, granted_by)
                      VALUES (:u, 'platform_admin', :u)"""), {"u": uid})
    print(f"    created {phone} as a platform admin")
PY

# ── go ───────────────────────────────────────────────────────────────
cat <<BANNER

  ${BOLD}API${OFF}      http://127.0.0.1:${API_PORT}
  ${BOLD}Docs${OFF}     http://127.0.0.1:${API_PORT}/docs        ${DIM}(development only)${OFF}
  ${BOLD}Console${OFF}  ${DIM}make dev-web, in another terminal${OFF}

  Sign in as ${BOLD}${DEV_PHONE}${OFF} — the code comes back in the response:

    ${DIM}CH=\$(curl -s localhost:${API_PORT}/api/v1/auth/otp/request \\
           -H 'content-type: application/json' -d '{"phone":"${DEV_PHONE}"}')
    curl -s localhost:${API_PORT}/api/v1/auth/otp/verify -H 'content-type: application/json' \\
      -d "{\\"challenge_xid\\":\\"\$(jq -r .challenge_xid <<<\$CH)\\",\\"code\\":\\"\$(jq -r .pilot_code <<<\$CH)\\"}"${OFF}

BANNER

step "Starting the API"
exec uvicorn app.api.main:app --reload --port "$API_PORT"
