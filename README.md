# GeoSWMM Gateway

A small FastAPI proxy that sits between the GeoSWMM AI add-in and OpenRouter / Qdrant. The trust boundary is here — server keys never leave the VPS.

See [GatewayServerPlan.md](GatewayServerPlan.md) for the spec and [CLAUDE.md](CLAUDE.md) for project conventions.

## Quick start (local dev)

```powershell
# 1. Configure
copy .env.example .env
# Open .env and fill in OPENROUTER_API_KEY, QDRANT_*, JWT_SECRET, POSTGRES_PASSWORD.

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

Deploy targets a single Ubuntu 24.04 VPS (4 vCPU / 4 GB / 20 GB) for a one-month trial. See §11 of the plan for the production Compose setup. Set `PUBLIC_HOSTNAME` in `.env` to your real domain and Caddy will provision Let's Encrypt automatically.

## Security

- Server-side keys (OpenRouter, Qdrant, JWT secret, Postgres password) live only in `.env` on the VPS.
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

**Bootstrap the first admin:**

```powershell
docker compose run --rm app gateway-admin promote admin@yourcompany.com
```

This sets `is_admin=TRUE` for an existing registered user. There is no "first request creates admin" magic — you must have the user registered first.

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
