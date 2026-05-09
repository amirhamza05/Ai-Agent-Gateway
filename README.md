# GeoSWMM Gateway

A small FastAPI proxy that sits between the GeoSWMM AI add-in and OpenRouter / Qdrant. The trust boundary is here — server keys never leave the VPS.

See [GatewayServerPlan.md](GatewayServerPlan.md) for the spec and [CLAUDE.md](CLAUDE.md) for project conventions.

## Quick start (local dev)

```powershell
# 1. Configure
copy .env.example .env
# Set JWT_SECRET, POSTGRES_PASSWORD, DATABASE_URL.
# OpenRouter + Qdrant credentials are entered later from the dashboard
# at /dashboard/settings — leave those blank in .env.

# 2. Start dependencies
docker compose up -d postgres redis

# 3. Run migrations
docker compose run --rm app alembic upgrade head

# 4. Start the app
docker compose up app
```

The app listens on `http://localhost:8000`. Verify:

```powershell
curl http://localhost:8000/healthz
```

## Project layout

```
.
├── GatewayServerPlan.md     # spec
├── CLAUDE.md                # conventions for Claude / contributors
├── docker-compose.yml
├── Caddyfile
├── .env.example
├── app/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/
│   ├── src/gateway/         # see §10 of the plan
│   └── tests/
└── .claude/                 # Claude Code agents, commands, settings
```

## Testing

```powershell
docker compose run --rm app pytest -v
```

## Deployment

Deploy targets a single Ubuntu 24.04 VPS (4 vCPU / 4 GB / 20 GB) for a one-month trial. See §11 of the plan for the production Compose setup, and [ops/README.md](ops/README.md) for the operational runbook (log cap, disk alarm, backups, log-reading recipes).

Bootstrap order on a fresh VPS:

1. `copy .env.example .env` and set: `JWT_SECRET`, `POSTGRES_PASSWORD`, `DATABASE_URL`, and `PUBLIC_HOSTNAME` (the domain Caddy will request a Let's Encrypt cert for). Upstream credentials (OpenRouter, Qdrant) are entered from the dashboard later — leave them blank in `.env`.
2. `docker compose up -d postgres redis`
3. `docker compose run --rm app alembic upgrade head` — migrations create the schema and seed a single admin user (`admin@gmail.com` / `password`). Nothing else is seeded; model pricing and upstream credentials are configured from the dashboard.
4. `docker compose up -d app caddy`
5. Sign in at `https://<PUBLIC_HOSTNAME>/dashboard/login` as `admin@gmail.com` / `password`. Then:
   - **Settings** — enter `OPENROUTER_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`. `CredentialStore` picks them up within 30 s (per-worker TTL cache) with no app restart.
   - **Models** — add the model pricing rows you want allow-listed. The gateway falls back to in-process bootstrap prices for `claude-opus-4.7` / `sonnet-4.6` / `haiku-4.5` / `text-embedding-3-*` until you do, and logs a warning.
   - For a public-facing deploy, create a fresh admin via **Users → New User** and either deactivate `admin@gmail.com` or rotate its `password_hash` directly with `psql`. The seeded credential is well-known.

Caddy provisions and renews Let's Encrypt certs automatically as long as `PUBLIC_HOSTNAME` resolves to the VPS and ports 80/443 are open.

## Security

- Server-side secrets (`JWT_SECRET`, `POSTGRES_PASSWORD`) live only in `.env` on the VPS.
- Upstream credentials (`OPENROUTER_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`) may live in `.env` **or** in the `gateway_settings` table (entered via `/dashboard/settings`). The `CredentialStore` resolves DB → env → error, so a value in either place is enough.
- The add-in only ever holds a per-user access + refresh token issued by `/auth/login`.
- Refresh tokens are stored as SHA-256 hashes; raw tokens are never persisted server-side.
- Run `/security-audit` (Claude command) before deployment.

## Safety nets

P4 lands the per-user safety nets from §8–§9 of the plan. These run
before any upstream call and are cheap by design — the rate-limit
check is one Redis EVALSHA, the cap check is one indexed SUM.

- **429 Too Many Requests** when a user exceeds `RATE_LIMIT_PER_MIN`.
  Per-user Redis token bucket (atomic Lua) so noisy users don't starve
  quiet ones. Response carries both a `Retry-After` header and a
  `detail.retry_after_sec` field for callers that read JSON bodies.
- **402 Payment Required** when a user's month-to-date `cost_usd` sum
  meets or exceeds their `monthly_usd_cap`. Response detail carries
  `cap_usd` and `spent_usd` so the add-in can render an accurate
  "you've used $X of $Y" message without a follow-up `/v1/usage`
  call.
- **413 Payload Too Large** when an inbound request body exceeds
  `MAX_BODY_BYTES * 4`. Enforced as ASGI middleware ahead of routing,
  so the rejection happens before auth runs.
- **Per-row body truncation** at `MAX_BODY_BYTES` for `request_body`
  and `response_body` in `request_log`. The original byte count is
  preserved in `request_bytes` / `response_bytes` so the audit row
  stays truthful.

`/v1/usage` is intentionally NOT rate-limited or capped — reading
your own usage should always be available.

Operational config (Docker log cap, host disk-usage alarm, backup
commands, log-reading recipes) lives in [ops/README.md](ops/README.md).

## Dashboard

The operator admin dashboard lives at `/dashboard/`. It requires a session cookie issued via `/dashboard/login`.

**First login (fresh deploy):** `admin@gmail.com` / `password` — seeded by migration `0007_seed_admin`. The seeded credential is intentionally well-known; for any public-facing deploy, create a replacement admin via **Users → New User** and deactivate this one.

**Promoting another user to admin** (post-bootstrap, e.g. when adding a teammate who registered via `/auth/register`):

```powershell
docker compose run --rm app gateway-admin promote teammate@example.com
```

**Login URL:** `http://localhost:8000/dashboard/login`

**What the operator can do:**

- **Users** — Create users (email + password + monthly USD cap), view spend, update caps, toggle admin access, deactivate accounts, regenerate API tokens. Newly created tokens are shown once on a "copy and close" page.
- **Models** — Add/edit/disable model pricing rows. The `model_pricing` table is the authoritative allow-list; changes take effect within 30 seconds (TTL of the in-process cache).
- **Logs** — Browse `request_log` with filters (user, endpoint, model, status, date range). Pagination defaults to 50 rows, max 200.
- **Reports** — Cost over time (bar chart), top users by spend, error rate over time, and latency percentiles (p50/p95/p99) by endpoint. Each report page fetches a `.json` endpoint client-side via Chart.js.

Sessions use a SHA-256-hashed cookie (never the raw token). CSRF tokens protect every POST form. All dashboard endpoints are excluded from the per-user rate limit and monthly cap enforcement.

## Status

Trial month — see §13 of the plan for the build phase plan (P1–P7).
P1–P4 are complete. Phase D (admin dashboard) is complete.
