# Gateway Server Plan — OpenRouter + Qdrant Proxy for GeoSWMM AI

**Status:** Proposed
**Last updated:** 2026-05-09
**Audience:** server implementer, add-in maintainer
**Scope:** one-month trial deployment on a 4 vCPU / 4 GB / 20 GB Ubuntu 24.04 VPS

A small backend that sits between the GeoSWMM AI add-in and the upstream model
providers. Your website holds the OpenRouter and Qdrant API keys; the add-in
holds only a per-user login token. Every request is logged with full bodies
for the trial month so you can audit traffic, compute cost, and debug.

---

## 1. Goals & Non-Goals

**Goals**

- Add-in never sees an OpenRouter or Qdrant key — only a user token from your site.
- Streaming chat responses pass through with no buffering (first-token latency stays low).
- Every request and response is logged for one month, including bodies (capped per row).
- One `docker compose up` deploys the whole stack on a fresh VPS.
- Survives a one-month trial on 20 GB of disk without manual cleanup.

**In scope (added after P5)**

- **Admin dashboard** at `/dashboard/*` — cookie-session auth gated by
  `users.is_admin`, lets the operator create users (and see their initial
  tokens once), edit `model_pricing` rows, browse `request_log` with
  filters, and see cost / top-users / errors / latency reports. Same
  FastAPI app, server-rendered Jinja2 + HTMX + Tailwind CDN. No SPA.

**Non-goals (intentionally deferred)**

- Multi-region, HA, or autoscaling.
- Long-term log retention or analytics warehouse.
- OAuth/social login — email + password is enough for the trial.
- Per-organisation billing — flat per-user monthly USD cap.

---

## 2. Architecture

```mermaid
flowchart LR
    subgraph User Machine
      Pro[ArcGIS Pro]
      AddIn[[GeoSWMM AI Add-in]]
    end

    subgraph VPS Ubuntu 24.04
      Caddy[Caddy<br/>TLS termination]
      App[FastAPI app<br/>uvicorn × 2]
      PG[(PostgreSQL 16)]
      RD[(Redis 7)]
    end

    OR[(OpenRouter)]
    QD[(Qdrant Cloud)]

    Pro --> AddIn
    AddIn -- HTTPS + Bearer token --> Caddy
    Caddy -- internal --> App
    App <--> PG
    App <--> RD
    App -- server-side OpenRouter key --> OR
    App -- server-side Qdrant key --> QD
```

The trust boundary is the gateway. OpenRouter and Qdrant secrets live only in
the VPS's `.env` file and never leave it.

---

## 3. Stack Choices

| Layer | Choice | Why this and not the alternatives |
|---|---|---|
| HTTP framework | **FastAPI** + `uvicorn` (2 workers) | Async-native — every chat stream pins one coroutine, not one OS worker. Flask sync would starve workers under streaming load. Quart works too but FastAPI has bigger ecosystem. |
| Upstream HTTP client | **httpx** async, with `client.stream(...)` | First-class SSE passthrough. Don't use `requests` — it's sync. |
| Main DB | **PostgreSQL 16** | JSONB for flexible request metadata, automatic TOAST/LZ compression for large text, mature ops. SQLite would bottleneck on concurrent log writes. |
| Cache + rate limit | **Redis 7** | Token-bucket rate limiting, refresh-token blocklist, optional embedding cache. |
| Reverse proxy + TLS | **Caddy 2** | Auto-HTTPS via Let's Encrypt with one config line. Simpler than Nginx for solo ops. |
| Auth | **JWT access (15 min) + opaque refresh token (30 d) hashed in DB** | Stateless access tokens, revocable refresh tokens. |
| Password hashing | **argon2id** via `argon2-cffi` | Modern default; bcrypt is fine but argon2 is preferred for new systems. |
| Migrations | **Alembic** | Standard with SQLAlchemy. |
| ORM | **SQLAlchemy 2.0 async** | Fine ergonomics; raw `asyncpg` is faster but not worth the boilerplate at this scale. |
| Logging | **structlog** → stdout → Docker → `docker logs` | One log line per request, JSON-formatted, easy to grep. |
| Container orchestration | **Docker Compose** | Single VPS, single host. Kubernetes is overkill. |

---

## 4. VPS Sizing Decision

Configured: **4 vCPU / 4 GB RAM / 20 GB SSD / 1 TB traffic / Ubuntu 24.04**.

| Resource | Headroom for one month |
|---|---|
| 4 vCPU | Plenty — workload is I/O-bound. Two uvicorn workers handle hundreds of concurrent streams. |
| 4 GB RAM | Adequate with explicit container limits (Postgres 1 GB, Redis 200 MB, App 1 GB). |
| 20 GB disk | OS + Docker images ≈ 10 GB. Postgres compression keeps `request_log` ≈ 4 KB/row. Supports ~50–100k requests/day with full bodies for 30 days. |
| 1 TB traffic | At ~30 KB per round-trip × 2 hops, supports ~16M small requests or ~1.5M long chat turns. |

For one-month trial, no partitioning, no off-box backup, no rollup tables.
Disk usage alarm and Docker log cap are the only safety nets needed.

---

## 5. Endpoint Contract

All authenticated endpoints require `Authorization: Bearer <access_token>`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/register` | Email + password → user row created. |
| POST | `/auth/login` | Returns `{access_token, refresh_token, expires_in}`. |
| POST | `/auth/refresh` | Refresh token → new access token. |
| POST | `/auth/logout` | Revoke refresh token. |
| POST | `/v1/messages` | **Streaming passthrough** to `https://openrouter.ai/api/v1/messages`. |
| POST | `/v1/embeddings` | Non-streaming passthrough to OpenRouter or embedding provider. |
| POST | `/v1/qdrant/search` | Forward `{collection, vector, limit, filter}` to Qdrant `/collections/{c}/points/search`. |
| POST | `/v1/qdrant/upsert` | Forward to Qdrant points upsert. (Optional — only if the add-in writes back.) |
| GET  | `/v1/usage` | Current month's `tokens_in/out`, `cost_usd`, request count for the caller. |
| GET  | `/healthz` | Liveness. No auth. |

**Wire format for `/v1/messages`** is byte-for-byte the Anthropic Messages API
shape (the same shape OpenRouter exposes). The add-in's `AnthropicClient` only
needs a different `ApiUrlFormat` and a different bearer token — no SDK changes.

---

## 6. Auth Flow

```mermaid
sequenceDiagram
    participant Add-in
    participant Gateway
    participant Postgres

    Add-in->>Gateway: POST /auth/login (email, password)
    Gateway->>Postgres: SELECT user WHERE email=?
    Gateway->>Gateway: argon2.verify(password, hash)
    Gateway->>Postgres: INSERT refresh_token (hash, expires_at)
    Gateway-->>Add-in: {access_token (15 min JWT), refresh_token, expires_in}

    Note over Add-in: stores both via DpapiSecretsStore

    Add-in->>Gateway: POST /v1/messages + Bearer access_token
    Gateway->>Gateway: verify JWT (signature + exp)
    Gateway-->>Add-in: SSE stream

    Note over Add-in: 15 minutes later, JWT expires

    Add-in->>Gateway: POST /v1/messages (401)
    Gateway-->>Add-in: 401 Unauthorized
    Add-in->>Gateway: POST /auth/refresh (refresh_token)
    Gateway->>Postgres: SELECT refresh_token WHERE hash=? AND revoked_at IS NULL
    Gateway-->>Add-in: new access_token
    Add-in->>Gateway: retry POST /v1/messages
```

Refresh tokens are stored **as a SHA-256 hash** in the DB; the raw token only
exists in the response and on the client. A leaked DB row cannot impersonate
the user.

---

## 7. Database Schema

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  monthly_usd_cap NUMERIC(10,4) NOT NULL DEFAULT 10.00,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE refresh_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON refresh_tokens (user_id);

CREATE TABLE request_log (
  id BIGSERIAL PRIMARY KEY,
  request_id UUID NOT NULL,
  user_id UUID REFERENCES users(id),
  endpoint TEXT NOT NULL,                -- 'messages' | 'embeddings' | 'qdrant.search' | ...
  model TEXT,
  tokens_in INT,
  tokens_out INT,
  cost_usd NUMERIC(10,6),
  status_code INT,
  error_code TEXT,
  latency_ms INT,
  client_version TEXT,
  client_ip INET,

  request_body  JSONB,                   -- truncated to MAX_BODY_BYTES
  response_body TEXT,                    -- streamed text, truncated to MAX_BODY_BYTES
  request_bytes  INT,                    -- original size before truncation
  response_bytes INT,

  meta JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON request_log (user_id, created_at DESC);
CREATE INDEX ON request_log (created_at);
```

`request_body` and `response_body` are TOAST-compressed automatically by
Postgres for values larger than ~2 KB, giving 3–5× shrinkage on chat-style
text. No extra app-side compression needed.

---

## 8. Body-Size & Disk Safety

Two hard caps prevent a single user from filling the disk.

**8.1 Per-row truncation**

```python
MAX_BODY_BYTES = 32_000

def truncate(text: str) -> tuple[str, int]:
    raw = text.encode("utf-8")
    if len(raw) > MAX_BODY_BYTES:
        return raw[:MAX_BODY_BYTES].decode("utf-8", errors="ignore") + "\n…[truncated]", len(raw)
    return text, len(raw)
```

Always store the **original size** in `request_bytes` / `response_bytes` so logs
remain truthful even when the body is clipped.

**8.2 Streaming tee**

The `/v1/messages` handler must yield each chunk to the client immediately
**and** accumulate (up to `MAX_BODY_BYTES`) for the log row. Pseudocode:

```python
async def upstream_stream():
    accumulated = bytearray()
    async with httpx_client.stream("POST", OPENROUTER_URL, json=body, headers=...) as resp:
        async for chunk in resp.aiter_raw():
            if len(accumulated) < MAX_BODY_BYTES:
                accumulated.extend(chunk[:MAX_BODY_BYTES - len(accumulated)])
            yield chunk                          # client gets it instantly
    await log_request(..., response_body=accumulated.decode("utf-8", "replace"))
```

Two rules: never `await resp.aread()` (kills first-token latency), and never
let `accumulated` grow unbounded.

**8.3 Disk-usage alarm (host cron)**

```bash
*/15 * * * * df /var/lib/docker | awk 'NR==2 && $5+0>80 {print $0}' \
  | curl -s -X POST -H 'Content-Type: application/json' \
    -d "@-" $DISCORD_WEBHOOK
```

At 80% disk usage you still have ~4 GB to react.

**8.4 Docker log cap**

`/etc/docker/daemon.json`:

```json
{ "log-driver": "local", "log-opts": { "max-size": "50m", "max-file": "3" } }
```

Without this, a chatty container can fill 20 GB on its own.

---

## 9. Rate Limiting & Cost Cap

**Per-user QPS** — Redis token bucket, e.g. 30 req/min per user. Reject with
`429 Too Many Requests` and a `Retry-After` header.

**Monthly USD cap** — before each upstream call:

```sql
SELECT COALESCE(SUM(cost_usd), 0)
FROM request_log
WHERE user_id = $1 AND created_at >= date_trunc('month', now());
```

If the result ≥ `users.monthly_usd_cap`, return `402 Payment Required`. The
add-in surfaces this as a friendly "monthly limit reached" message.

Cost is computed from a small in-process price table:

```python
PRICES_PER_MTOKEN = {
    "claude-opus-4-7":    {"in": 15.00, "out": 75.00},
    "claude-sonnet-4-6":  {"in":  3.00, "out": 15.00},
    "claude-haiku-4-5":   {"in":  1.00, "out":  5.00},
    # ... extend as needed
}
```

---

## 10. Project Layout

```
geoswmm-gateway/
├── docker-compose.yml
├── Caddyfile
├── .env.example
├── .gitignore
├── README.md
└── app/
    ├── Dockerfile
    ├── pyproject.toml
    ├── alembic.ini
    ├── migrations/
    └── src/gateway/
        ├── __init__.py
        ├── main.py                 # FastAPI factory, lifespan, middleware wiring
        ├── config.py               # pydantic-settings, reads .env
        ├── auth/
        │   ├── routes.py           # /auth/*
        │   ├── jwt.py
        │   ├── passwords.py        # argon2 wrapper
        │   └── deps.py             # require_user dependency
        ├── routes/
        │   ├── messages.py         # POST /v1/messages — streaming
        │   ├── embeddings.py       # POST /v1/embeddings
        │   ├── qdrant.py           # POST /v1/qdrant/*
        │   └── usage.py            # GET /v1/usage
        ├── upstream/
        │   ├── openrouter.py       # async httpx client + key
        │   └── qdrant.py           # async httpx client + key
        ├── logging_mw.py           # writes one request_log row per call
        ├── ratelimit.py            # Redis token bucket
        ├── billing.py              # PRICES_PER_MTOKEN + cap check
        ├── truncate.py             # MAX_BODY_BYTES helpers
        └── db/
            ├── models.py           # SQLAlchemy
            └── session.py          # async engine
```

---

## 11. Deployment Files

**docker-compose.yml**

```yaml
services:
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config

  app:
    build: ./app
    restart: unless-stopped
    env_file: .env
    depends_on: [postgres, redis]
    deploy:
      resources:
        limits: { memory: 1g }

  postgres:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_DB: gateway
      POSTGRES_USER: gateway
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: [pgdata:/var/lib/postgresql/data]
    command: >
      postgres
      -c shared_buffers=512MB
      -c effective_cache_size=1GB
      -c work_mem=16MB
      -c max_connections=50
    deploy:
      resources:
        limits: { memory: 1g }

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --maxmemory 128mb --maxmemory-policy allkeys-lru
    volumes: [redisdata:/data]
    deploy:
      resources:
        limits: { memory: 200m }

volumes:
  caddy_data: {}
  caddy_config: {}
  pgdata: {}
  redisdata: {}
```

**Caddyfile**

```caddy
api.your-domain.com {
    reverse_proxy app:8000
}
```

**.env (server-side, never committed)**

```
POSTGRES_PASSWORD=...
DATABASE_URL=postgresql+asyncpg://gateway:...@postgres:5432/gateway
REDIS_URL=redis://redis:6379/0
JWT_SECRET=...
OPENROUTER_API_KEY=sk-or-...
QDRANT_URL=https://....cloud.qdrant.io
QDRANT_API_KEY=...
ALLOWED_MODELS=claude-opus-4-7,claude-sonnet-4-6,claude-haiku-4-5
DEFAULT_MONTHLY_USD_CAP=10.00
MAX_BODY_BYTES=32000
```

---

## 12. Add-in Side Changes

Touch points in this repo:

| File | Change |
|---|---|
| [GeoSWMMAI/Agent/ProviderConfig.cs](../GeoSWMMAI/Agent/ProviderConfig.cs) | Add `BackendBaseUrl` and `UserAccessToken` / `UserRefreshToken`. Deprecate the direct OpenRouter key path or hide it behind a "self-hosted" toggle. |
| [GeoSWMMAI/Agent/ChatAgent.cs](../GeoSWMMAI/Agent/ChatAgent.cs) | Construct `AnthropicClient` with `ApiUrlFormat = $"{BackendBaseUrl}/v1/{{0}}/{{1}}"` (or whichever overload the SDK exposes) and the user access token as bearer. No other changes — wire format is identical. |
| New `GeoSWMMAI/Agent/GatewayClient.cs` | Thin `HttpClient` wrapper used by the embedding + Qdrant tools. Handles 401 → refresh → retry once. Adds `X-Client-Request-Id` header so server logs and add-in `ToolTrace` rows can be correlated. |
| [GeoSWMMAI/Tools/SearchGeoswmmDocsTool.cs](../GeoSWMMAI/Tools/SearchGeoswmmDocsTool.cs) | Switch HTTP base from OpenRouter/Qdrant direct URLs to gateway endpoints. |
| [GeoSWMMAI/DockPanes/ApiKeyPromptWindow.xaml](../GeoSWMMAI/DockPanes/ApiKeyPromptWindow.xaml) + .cs | Replace API-key textbox with email + password fields (or a "Sign in" button that opens browser-based OAuth later). |
| [GeoSWMMAI/Secrets/DpapiSecretsStore.cs](../GeoSWMMAI/Secrets/DpapiSecretsStore.cs) | No code change — same DPAPI envelope, different secret payload (now `{access_token, refresh_token, expires_at}` JSON). |

The Pro SDK rules in [CLAUDE.md](../CLAUDE.md) don't change. Networking already
runs off the MCT.

---

## 13. Build Order (phased)

Each phase is one focused work session and lands behind a clear demo.

| Phase | Outcome | Demo |
|---|---|---|
| **P1 — Skeleton** | `docker compose up` runs Caddy + FastAPI hello-world + Postgres + Redis on the VPS, reachable at `https://api.your-domain.com/healthz` over real TLS. | `curl https://api.your-domain.com/healthz` returns `{"ok":true}`. |
| **P2 — Auth** | `/auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout` work with JWT + argon2 + refresh-token rotation. | Login from `httpie`, hit a protected `/v1/usage` endpoint with the bearer token. |
| **P3 — Messages passthrough** | `/v1/messages` streams from OpenRouter to the client and writes a `request_log` row with body + tokens + cost. | `curl --no-buffer` against the gateway prints SSE chunks identical to direct OpenRouter; a row appears in `request_log`. |
| **P4 — Safety nets** | Redis token-bucket rate limit, monthly cost cap check, body truncation, Docker log cap, host disk-usage alert. | A loop blasting 100 req/sec gets 429s; a fake user with `monthly_usd_cap=0.01` gets 402. |
| **P5 — Embeddings + Qdrant** | `/v1/embeddings`, `/v1/qdrant/search`, `/v1/qdrant/upsert` proxy and log. | The add-in's `SearchGeoswmmDocsTool` runs end-to-end via the gateway. |
| **P6 — Add-in cutover** | `ProviderConfig` + `ChatAgent` + `ApiKeyPromptWindow` updated. Add-in logs in once, holds tokens in DPAPI, talks only to the gateway. | Fresh Pro install with no OpenRouter key, full chat session works. |
| **P7 — Trial-end backup** | `pg_dump` script + storage-box upload. | Weekly dump file appears off-box; restore tested locally. |

Phases P1–P4 are the critical path; P5–P7 can land in any order after that.

---

## 14. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Disk fills before month end | Body truncation (32 KB), Docker log cap, 15-min disk alarm. |
| OpenRouter key leaks | Key only in `.env` on VPS; `.env` is in `.gitignore`; no key in any container image layer. Rotate at trial end. |
| One user burns the OpenRouter budget | Per-user monthly USD cap enforced before every upstream call. Sane default (e.g. $10). |
| Gateway downtime → all add-ins dark | Trial-only — accept the risk. Caddy + Compose `restart: unless-stopped` covers single-process crashes; full VPS outage is on the user. |
| Streaming buffered by reverse proxy | Caddy doesn't buffer SSE by default. Verified by checking first-byte time matches direct OpenRouter. Don't put Cloudflare in front in proxy mode without disabling buffering. |
| Refresh token theft | Stored only as SHA-256 hash; rotation on every use; `revoked_at` blacklist. |
| Password breaches via reused passwords | argon2id with default cost; require minimum 12 chars on register; consider HIBP API check later. |
| Trial data lost when VPS is destroyed | Weekly `pg_dump` to off-box storage during the trial. |

---

## 15. After the Trial

When the month is over, before tearing down the VPS:

1. **Final dump**
   ```bash
   docker exec postgres pg_dump -U gateway --format=custom gateway > final_$(date +%F).dump
   ```
2. **Move to cold storage** — Backblaze B2, S3 Glacier, or local archive.
3. **Decide next step** based on what the logs show:
   - If traffic was light and stable → keep the same VPS.
   - If traffic grew → 8 GB RAM + 80 GB disk upgrade, add `request_log` partitioning and a `usage_daily` rollup.
   - If a different cost shape emerged (e.g. embeddings dominated) → revisit the price table and possibly add caching for repeat embeddings.

---

## 16. Open Questions

These are intentionally left for the implementer to resolve in P1/P2:

- Domain name for the gateway (`api.your-domain.com` vs. subdomain of marketing site).
- Whether to ship `/auth/register` open or invite-only for the trial.
- Add-in UX for "session expired" — silent refresh works in 99% of cases, but the 1% that fails needs a visible re-login prompt.
- Whether to log the full Anthropic system prompt (cache-control payload) or strip it before storage to reduce row size.

### Resolved

- **Embeddings provider**: OpenRouter (chosen during P5; same auth + transport as Messages, simplest implementation). Default model `openai/text-embedding-3-small`. If usage profile shifts toward heavy embedding workload, revisit Voyage or self-hosted (cost data in `request_log.cost_usd` rolled up by `endpoint='embeddings'`).
