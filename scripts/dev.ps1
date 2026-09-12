# One command from a fresh clone to a signed-in developer, on Windows.
#
#     .\scripts\dev.ps1
#
# The PowerShell twin of `scripts/dev.sh`. Same steps, same order, same
# guarantees: starts PostgreSQL and Redis, builds the virtualenv, migrates,
# creates a first account, prints how to sign in as it, and runs the API with
# reload. Nothing needs to exist first.
#
# It exists because `make dev` needs `make` and bash, and stock Windows has
# neither. Telling somebody to install WSL to run a Python web server on their
# own laptop is a fair answer to give once and a bad one to build on.
#
# WSL2 remains the better environment if you want the rest of the toolchain —
# `make ci`, the shell scripts, the backup runbook — and Docker Desktop is
# already running a Linux VM either way. This file is for the common case of
# opening the repository in PowerShell and wanting it to run.

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Step($text) { Write-Host '==> ' -ForegroundColor Blue -NoNewline; Write-Host $text }
function Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

# ── configuration, all of it defaulted ───────────────────────────────
# Every one of these is an override, none is a requirement.
if (-not $env:ENVIRONMENT)       { $env:ENVIRONMENT = 'development' }
if (-not $env:DATABASE_URL)      { $env:DATABASE_URL = 'postgresql+psycopg://postgres@127.0.0.1:55432/ielts' }
if (-not $env:REDIS_URL)         { $env:REDIS_URL = 'redis://127.0.0.1:6399/0' }
if (-not $env:PILOT_OPEN_SIGNIN) { $env:PILOT_OPEN_SIGNIN = 'true' }
if (-not $env:API_PORT)          { $env:API_PORT = '8000' }
if (-not $env:DEV_PHONE)         { $env:DEV_PHONE = '+998901234567' }

if ($env:ENVIRONMENT -ne 'development') {
    Write-Error "refusing: ENVIRONMENT is '$($env:ENVIRONMENT)'. This script seeds an account and turns on open sign-in. It is for laptops only."
    exit 1
}

# ── the virtualenv ───────────────────────────────────────────────────
$venvPython = Join-Path (Get-Location) '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Step 'Creating .venv'
    # `py -3.12` first: the Windows launcher is how a machine with several
    # Pythons is asked for a specific one, and `python` on PATH is as likely to
    # be 3.11 or the Store stub that opens a shop page.
    $created = $false
    foreach ($cmd in @(@('py', '-3.13'), @('py', '-3.12'), @('python', $null))) {
        $exe, $arg = $cmd
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $probe = if ($arg) { @($arg, '-c', 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)') }
                 else      { @('-c', 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)') }
        & $exe @probe 2>$null
        if ($LASTEXITCODE -eq 0) {
            $mk = if ($arg) { @($arg, '-m', 'venv', '.venv') } else { @('-m', 'venv', '.venv') }
            & $exe @mk
            $created = $true
            break
        }
    }
    if (-not $created) {
        Write-Error 'need Python 3.12+. Install it from python.org (tick "Add to PATH") and run this again.'
        exit 1
    }
}

# The venv's interpreter is called directly rather than activated. Activation
# needs an execution policy that a default Windows install does not grant, and
# failing on `Activate.ps1` would be a confusing way to discover that.
$py = $venvPython

& $py -c 'import app' 2>$null
if ($LASTEXITCODE -ne 0) {
    Step 'Installing dependencies'
    Note 'first run only; a minute or two'
    # The same two steps as `make install` and dev.sh: the lock, hash-checked,
    # then the package editable with `--no-deps` — never `-e '.[dev]'`.
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet --require-hashes -r requirements-dev.txt
    & $py -m pip install --quiet -e . --no-deps
    if ($LASTEXITCODE -ne 0) { Write-Error 'dependency install failed'; exit 1 }
}

# ── services ─────────────────────────────────────────────────────────
function Test-Database {
    & $py scripts/dev_seed.py ping 2>$null
    return $LASTEXITCODE -eq 0
}

if (Test-Database) {
    Step 'PostgreSQL already reachable'
    Note $env:DATABASE_URL
} else {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Error 'PostgreSQL is not reachable at DATABASE_URL and docker is not installed. Start PostgreSQL 16 and Redis yourself, or set DATABASE_URL and REDIS_URL to where they already are.'
        exit 1
    }
    Step 'Starting PostgreSQL and Redis'
    # Named explicitly. `docker-compose.dev.yml` also declares the API, both
    # workers and the console — a bare `up` would start an API on :8000 and this
    # script would then fail to bind the port it just lost.
    docker compose -f docker-compose.dev.yml up -d --wait postgres redis
    if ($LASTEXITCODE -ne 0) { Write-Error 'docker compose failed — is Docker Desktop running?'; exit 1 }
    $ok = $false
    foreach ($i in 1..60) { if (Test-Database) { $ok = $true; break }; Start-Sleep -Seconds 1 }
    if (-not $ok) { Write-Error "the database did not come up; try 'docker compose -f docker-compose.dev.yml down -v'"; exit 1 }
}

# ── schema ───────────────────────────────────────────────────────────
Step 'Applying migrations'
& $py -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Write-Error 'migrations failed'; exit 1 }

# ── a first account ──────────────────────────────────────────────────
Step 'Checking for an account'
& $py scripts/dev_seed.py seed $env:DEV_PHONE
if ($LASTEXITCODE -ne 0) { Write-Error 'seeding failed'; exit 1 }

# ── go ───────────────────────────────────────────────────────────────
$port = $env:API_PORT
$phone = $env:DEV_PHONE
Write-Host ''
Write-Host '  API      ' -NoNewline; Write-Host "http://127.0.0.1:$port"
Write-Host '  Docs     ' -NoNewline; Write-Host "http://127.0.0.1:$port/docs" -NoNewline
Write-Host '        (development only)' -ForegroundColor DarkGray
Write-Host '  Console  ' -NoNewline; Write-Host '.\scripts\dev-web.ps1, in another terminal' -ForegroundColor DarkGray
Write-Host ''
Write-Host "  Sign in as $phone — the code comes back in the response."
Write-Host '  In another PowerShell:' -ForegroundColor DarkGray
Write-Host ''
# Invoke-RestMethod, not curl: on Windows PowerShell 5.1 `curl` is an alias for
# Invoke-WebRequest with entirely different arguments, so a curl line copied
# from the Linux banner fails in a way that looks like the server is broken.
Write-Host "    `$ch = Invoke-RestMethod -Method Post http://127.0.0.1:$port/api/v1/auth/otp/request ``" -ForegroundColor DarkGray
Write-Host "            -ContentType application/json -Body '{\"phone\":\"$phone\"}'" -ForegroundColor DarkGray
Write-Host "    `$s  = Invoke-RestMethod -Method Post http://127.0.0.1:$port/api/v1/auth/otp/verify ``" -ForegroundColor DarkGray
Write-Host "            -ContentType application/json ``" -ForegroundColor DarkGray
Write-Host "            -Body (@{challenge_xid=`$ch.challenge_xid; code=`$ch.pilot_code} | ConvertTo-Json)" -ForegroundColor DarkGray
Write-Host "    `$s.access_token" -ForegroundColor DarkGray
Write-Host ''

Step 'Starting the API'
& $py -m uvicorn app.api.main:app --reload --port $port
