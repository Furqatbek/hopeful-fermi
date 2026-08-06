#!/usr/bin/env bash
# The admin console, with the same promise as `dev.sh`: nothing first.
#
#     make dev-web
#
# Installs from the lockfile if `node_modules` is missing and regenerates the
# typed client if it is stale, then runs vite on :5173 with /api proxied to the
# API on :8000.
#
# **The codegen step is not a convenience.** `web/src/api/schema.d.ts` is
# generated from `openapi/openapi.yaml` and committed, and `make
# web-codegen-check` fails the build when the two disagree. Running the console
# against a stale client is the trap that check exists to close: the compiler
# happily types a field the server stopped sending, and the failure surfaces as
# `undefined` in front of a teacher rather than as a red build.

set -euo pipefail
cd "$(dirname "$0")/../web"

BLUE=$'\033[34m'; DIM=$'\033[2m'; OFF=$'\033[0m'
step() { printf '%s==>%s %s\n' "$BLUE" "$OFF" "$1"; }

command -v npm >/dev/null 2>&1 || {
    echo "need Node 22 and npm on PATH" >&2; exit 1; }

if [ ! -d node_modules ]; then
    step "Installing console dependencies"
    npm ci
fi

# Cheap to regenerate and expensive to get wrong, so it happens every time
# rather than only when a heuristic says the contract moved.
step "Regenerating the typed client from openapi/openapi.yaml"
npm run --silent codegen

printf '%s    the API must be running — make dev, in another terminal%s\n' "$DIM" "$OFF"
step "Starting the console on http://127.0.0.1:5173"
exec npm run dev
