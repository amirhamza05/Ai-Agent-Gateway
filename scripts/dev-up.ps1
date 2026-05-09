# scripts/dev-up.ps1
# ------------------------------------------------------------------------
# Local dev bring-up for the GeoSWMM Gateway.
#
# What it does:
#   1. Verifies .env exists (refuse to run with missing config).
#   2. Starts postgres + redis in the background.
#   3. Runs `alembic upgrade head` (no-op until P2 lands the first migration).
#   4. Starts the app in the foreground so logs stream to your terminal.
#
# Run from the repo root:
#   .\scripts\dev-up.ps1
# ------------------------------------------------------------------------

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# Resolve repo root from this script's location so the script works no
# matter where the user invokes it from.
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Test-Path -LiteralPath ".env")) {
    Write-Error @"
.env is missing. Copy .env.example and fill in real values:

    copy .env.example .env

Required keys: POSTGRES_PASSWORD, DATABASE_URL, JWT_SECRET, OPENROUTER_API_KEY,
QDRANT_URL, QDRANT_API_KEY. See CLAUDE.md > 'Local development'.
"@
    exit 1
}

Write-Host "==> Starting postgres and redis (detached)..." -ForegroundColor Cyan
docker compose up -d postgres redis
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Running database migrations..." -ForegroundColor Cyan
docker compose run --rm app alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Warning "alembic upgrade head exited with code $LASTEXITCODE."
    Write-Warning "(Expected to no-op until Phase 2 adds the first migration.)"
}

Write-Host "==> Starting app (Ctrl+C to stop)..." -ForegroundColor Cyan
docker compose up app
exit $LASTEXITCODE
