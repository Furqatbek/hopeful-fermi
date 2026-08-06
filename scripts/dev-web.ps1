# The admin console, on Windows. The twin of `scripts/dev-web.sh`.
#
#     .\scripts\dev-web.ps1
#
# Installs from the lockfile if `node_modules` is missing, regenerates the typed
# client from `openapi/openapi.yaml`, and runs vite on :5173 with /api proxied
# to the API on :8000.
#
# The codegen step is not a convenience: the client is generated and committed,
# and `make web-codegen-check` fails the build when the two disagree. Running
# the console against a stale client is the trap that check exists to close.

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..\web')

function Step($text) { Write-Host '==> ' -ForegroundColor Blue -NoNewline; Write-Host $text }

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Error 'need Node 22 and npm on PATH. https://nodejs.org'
    exit 1
}

if (-not (Test-Path node_modules)) {
    Step 'Installing console dependencies'
    npm ci
    if ($LASTEXITCODE -ne 0) { Write-Error 'npm ci failed'; exit 1 }
}

Step 'Regenerating the typed client from openapi/openapi.yaml'
npm run --silent codegen
if ($LASTEXITCODE -ne 0) { Write-Error 'codegen failed'; exit 1 }

Write-Host '    the API must be running — .\scripts\dev.ps1, in another terminal' -ForegroundColor DarkGray
Step 'Starting the console on http://127.0.0.1:5173'
npm run dev
