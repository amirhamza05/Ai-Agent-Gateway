---
description: Tail container logs or query the request_log table
argument-hint: [app|caddy|postgres|redis|db]
---

Inspect Gateway logs.

- `app` (default) → `docker compose logs -f --tail=200 app`
- `caddy` → `docker compose logs -f --tail=200 caddy`
- `postgres` → `docker compose logs -f --tail=200 postgres`
- `redis` → `docker compose logs -f --tail=200 redis`
- `db` → open a `psql` shell against the gateway DB and show the last 20 `request_log` rows:
  ```
  docker compose exec postgres psql -U gateway -d gateway -c \
    "SELECT id, request_id, user_id, endpoint, model, status_code, latency_ms, created_at
     FROM request_log ORDER BY created_at DESC LIMIT 20;"
  ```

Container logs are bounded (50 MB × 3 files). The `request_log` table is the authoritative audit trail — prefer querying it for anything older than a few hours.
