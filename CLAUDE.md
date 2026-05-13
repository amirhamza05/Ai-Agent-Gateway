# GeoSWMM Gateway — Claude Project Notes

A FastAPI gateway that proxies the GeoSWMM AI add-in to OpenRouter and a vector store (Qdrant Cloud today, local pgvector after the migration in [docs/qdrant-to-pgvector-migration.md](docs/qdrant-to-pgvector-migration.md)). The trust boundary lives here: server-side keys never reach the add-in.

Read [GatewayServerPlan.md](GatewayServerPlan.md) for the authoritative spec. This file is project conventions for Claude — it is **not** a substitute for the plan.

## Stack

- **Python 3.12** + **FastAPI** + **uvicorn** (2 workers in prod, 1 in dev)
- **httpx** async, with `client.stream(...)` for SSE passthrough
- **PostgreSQL 16** via **SQLAlchemy 2.0 async** + **asyncpg** driver, image `pgvector/pgvector:pg16`
- **pgvector** extension for the local vector backend (replacing Qdrant Cloud behind `PGVECTOR_ENABLED`)
- **Redis 7** for rate limiting and refresh-token blocklist
- **Caddy 2** for TLS termination
- **Docker Compose** for the whole stack
- **Alembic** for migrations
- **structlog** for JSON logging
- **argon2-cffi** for password hashing
- **PyJWT** for access tokens
- **pytest** + **pytest-asyncio** + **httpx.AsyncClient** for tests

## Project layout

Code lives under `app/src/gateway/`. The layout in §10 of the plan is authoritative — do not invent new top-level modules without updating the plan.

```
app/src/gateway/
├── main.py            # FastAPI factory, lifespan, middleware wiring
├── config.py          # pydantic-settings, reads .env
├── auth/              # routes.py, jwt.py, passwords.py, deps.py
├── routes/            # messages.py, embeddings.py, qdrant.py, usage.py
├── upstream/          # openrouter.py, qdrant.py, pgvector.py — async wrappers
├── dashboard/         # routes.py, vectordb.py, templates/ — admin UI (Jinja2 + HTMX)
├── logging_mw.py      # one request_log row per call
├── ratelimit.py       # Redis token bucket
├── billing.py         # PRICES_PER_MTOKEN + cap check
├── truncate.py        # MAX_BODY_BYTES helpers
└── db/                # models.py (incl. Embedding), session.py (pgvector codec)
```

## Conventions

### Async everywhere
Every I/O path is async. No `requests`, no sync SQLAlchemy session, no blocking file reads in the hot path. If you find yourself reaching for a sync library, look harder for the async one (e.g. `aiofiles`, `httpx`, `redis.asyncio`).

### Streaming is sacred
For `/v1/messages`, **never** call `await resp.aread()` and **never** buffer the full upstream response before yielding. The handler must yield each chunk to the client immediately while a parallel `bytearray` accumulates up to `MAX_BODY_BYTES` for the log row. This is spelled out in §8.2 — re-read it before changing the streaming code.

Caddy does not buffer SSE by default. If you add another reverse proxy (Cloudflare, Nginx) in front, disable buffering explicitly.

### Secrets
- Server keys (OpenRouter, Qdrant, JWT secret, Postgres password) live only in `.env` on the VPS.
- `.env` is in `.gitignore`. `.env.example` is the template.
- Never log secret values. Never bake them into Docker image layers (use `env_file:`, not `ENV` in Dockerfile).
- Refresh tokens are stored as **SHA-256 hashes** in `refresh_tokens.token_hash`. The raw token only exists in the response body and on the client.

### Database
- Use SQLAlchemy 2.0 style (`select(...)`, `session.execute(...)`). No legacy `query()`.
- Every schema change is an Alembic migration. Never edit a past migration after it's been applied; add a new one.
- `request_body` and `response_body` rely on Postgres TOAST/LZ compression — do not pre-compress in app code.
- Always store the **original size** in `request_bytes` / `response_bytes` even when the body is truncated.
- pgvector codec: `register_vector` is wired to the engine's pool `"connect"` event in `db/session.py`. Do not re-register per query, and do not bypass `create_engine` when standing up a new engine — tests included.

### Logging
- One JSON log line per request via `structlog`.
- Structured fields: `request_id`, `user_id`, `endpoint`, `model`, `status_code`, `latency_ms`. Add fields by extending the bound logger, never by string-formatting.
- The `request_log` table is the authoritative audit trail. `docker logs` is for live debugging.

### Auth
- Access tokens: JWT, 15-minute expiry, signed with `JWT_SECRET` (HS256).
- Refresh tokens: opaque random 32+ bytes, hashed with SHA-256 in DB, 30-day expiry, single-use (rotate on every refresh and revoke the old one).
- Argon2id with default cost from `argon2-cffi`. Don't downtune cost without a benchmark.
- Minimum password length: 12 characters at register time.

### Rate limiting & cost cap
- Per-user QPS via Redis token bucket. Reject with `429` and `Retry-After`.
- Monthly USD cap checked **before** every upstream call. Reject with `402 Payment Required` when exceeded.
- Cost is computed from `billing.PRICES_PER_MTOKEN`. Add new model rows when you add new models to `ALLOWED_MODELS`.

### Error responses
Use FastAPI `HTTPException` with structured detail:
```python
raise HTTPException(status_code=402, detail={"error": "monthly_cap_exceeded", "cap_usd": cap})
```
The add-in surfaces `detail.error` as a friendly message — keep error codes stable across versions.

### Tests
- `pytest-asyncio` with `asyncio_mode = "auto"`.
- Integration tests hit a real Postgres + Redis (use Compose-launched test services, not mocks). Fast unit tests for pure functions (truncation, cost math) can stay in-process.
- Mock OpenRouter via `respx` so tests don't burn real API budget. Streaming tests verify chunks arrive **incrementally** — assert on per-chunk timestamps, not just final body.
- `/v1/qdrant/*` tests are parametrized on `backend ∈ {"qdrant", "pgvector"}` — the qdrant variant uses `respx`, the pgvector variant hits the real test DB. Keep both green when changing either backend.
- `TEST_DATABASE_URL` must point at a different database than `DATABASE_URL`; `conftest.py` refuses to start otherwise because the suite truncates tables between tests.

## Working with this project

### Local development
```powershell
# First time
copy .env.example .env
# fill in OPENROUTER_API_KEY, QDRANT_*, generate JWT_SECRET, POSTGRES_PASSWORD
docker compose up -d postgres redis
docker compose run --rm app alembic upgrade head
docker compose up app
```

The `app` service exposes `:8000` directly in dev (no Caddy needed locally). Caddy + TLS is only for the VPS.

### Migrations
```powershell
docker compose run --rm app alembic revision --autogenerate -m "add foo"
docker compose run --rm app alembic upgrade head
```

Always inspect the autogenerated migration before applying it — Alembic mis-detects index changes occasionally.

### Adding a new model to the price table
1. Add the row to `app/src/gateway/billing.py::PRICES_PER_MTOKEN`.
2. Add the model name to `ALLOWED_MODELS` in `.env.example` and document.
3. No migration needed.

### Vector backend (pgvector vs. Qdrant Cloud)
- Feature flag: `PGVECTOR_ENABLED` (default `false`). When false the `/v1/qdrant/*` routes proxy to Qdrant Cloud via `app.state.qdrant_client`. When true they call `gateway.upstream.pgvector` directly against the local Postgres.
- Both paths emit the same `request_log` row shape and the same `endpoint` string. `meta.backend` is set to `"qdrant"` or `"pgvector"` so the switchover is greppable in the logs.
- The pgvector filter DSL covers only the `must` / `must_not` / `match` / `range` shapes used by the add-in. Anything outside that subset returns `400 {"error": "invalid_filter"}`. Extend the DSL deliberately — see [docs/qdrant-to-pgvector-migration.md](docs/qdrant-to-pgvector-migration.md) §6.
- Don't widen the `Embedding` row beyond 1536 dimensions. If a future provider needs a different size, add a second table — the HNSW index is dimension-locked.

## What NOT to do

- Don't add features beyond §1 of the plan during the trial month. The "non-goals" list is intentional — admin UI, OAuth, multi-region, etc. are deferred.
- Don't pre-compress bodies before insert — Postgres TOAST handles it.
- Don't `await response.aread()` on streaming endpoints. Ever.
- Don't log secrets, password hashes, or full bearer tokens. Log a `request_id` and let the operator join on `request_log` with `psql`.
- Don't introduce a sync DB session "just for this script." Use the async session.
- Don't bypass the gateway from the add-in. Even debug builds go through the gateway so logs stay complete.
- Don't put Cloudflare in proxy mode in front of `/v1/messages` without disabling buffering — it kills first-token latency silently.

## Phase plan

P1 → P7 from §13 of the plan. Critical path is P1–P4. Each phase ends with a runnable demo:

1. **P1** — `curl /healthz` over Compose
2. **P2** — Login from `httpie`, hit a protected endpoint with the bearer
3. **P3** — `curl --no-buffer` against `/v1/messages` prints SSE chunks; `request_log` row exists
4. **P4** — Rate-limit loop gets 429s; user with `monthly_usd_cap=0.01` gets 402
5. **P5** — `/v1/embeddings` and `/v1/qdrant/*` proxy and log
6. **P6** — Add-in cutover (in the GeoSWMMAI repo, not here)
7. **P7** — `pg_dump` + off-box backup

Don't skip a phase's demo. If the demo doesn't work, the phase isn't done.

## Open decisions (resolve during P1/P2)

These are tracked in §16 of the plan:
- Domain name (`api.your-domain.com` or other)
- `/auth/register` open or invite-only for the trial
- Embeddings provider (OpenRouter vs. Voyage vs. OpenAI)
- "Session expired" UX in the add-in
- Whether to log full system prompts or strip them

When you resolve any of these, update §16 of the plan with the decision and rationale.

## Dashboard

Bootstrap: migration `0007_seed_admin` seeds a single admin (`admin@gmail.com` / `password`) at `alembic upgrade head` time so a fresh deploy can sign in directly. To promote a teammate later, register them via `/auth/register` then `docker compose run --rm app gateway-admin promote <email>`. The well-known seeded credential is for trial-month convenience — replace it on any public-facing deploy.

Login URL: `http://localhost:8000/dashboard/login` — cookie-based session, 8-hour expiry, refreshed on activity. Sessions are backed by `dashboard_sessions` table (SHA-256 hashed token, never the raw value).

CSRF protection on every POST form (except `/dashboard/login`). Token is an `itsdangerous.URLSafeTimedSerializer` payload bound to `(user_id, session_id)`. Invalid CSRF returns `400 {"error": "csrf_invalid"}`.

Pricing cache: 30-second TTL in-process dict. Invalidated after every dashboard mutation to `model_pricing`. The cache is per-worker; a multi-worker deploy may see a window of up to 30s where one worker hasn't picked up a change.

Dashboard endpoints are excluded from `enforce_rate_limit` and `enforce_monthly_cap` — those apply only to `/v1/*` traffic.
