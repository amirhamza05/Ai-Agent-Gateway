# Qdrant Cloud → pgvector Migration Plan

**Status:** Draft
**Owner:** unassigned
**Target phase:** post-P5 maintenance (no impact on P1–P4 critical path)
**Last updated:** 2026-05-14

## 1. Why

We currently proxy `/v1/qdrant/*` to a managed Qdrant Cloud cluster. The trial-month
workload (a handful of small collections for `SearchGeoswmmDocsTool`) does not need a
separate vector database. Folding embeddings into the existing Postgres 16 instance via
the `pgvector` extension:

- Removes one external dependency, one API key to rotate, one set of network credentials.
- Brings embeddings into `pg_dump` / off-box backups automatically (P7).
- Keeps a single async connection pool (`asyncpg`) — no second client to wire into
  the FastAPI lifespan.
- Costs nothing in additional infra: pgvector ships as a Postgres extension and the
  HNSW index is competitive with Qdrant up to single-digit-millions of vectors.

This change resolves the embeddings-provider open decision in `GatewayServerPlan.md` §16
for the **storage** side (the embedding *generation* provider is a separate choice).

## 2. Non-goals

- No change to OpenRouter, auth, dashboard, rate limiting, or billing.
- No change to the embeddings *generation* provider — `/v1/embeddings` is untouched.
- No multi-tenant vector isolation beyond what the existing `user_id` foreign key gives us.
- No hybrid (sparse + dense) search. We replicate today's dense-only behavior.

## 3. Architecture

### Before

```
add-in ──HTTPS──► /v1/qdrant/search ──┐
                                       ├──► httpx ──HTTPS──► Qdrant Cloud REST
add-in ──HTTPS──► /v1/qdrant/upsert ──┘
```

`app.state.qdrant_client` is an `httpx.AsyncClient`; credentials come from the
`gateway_settings` table via `CredentialStore` (`qdrant_url`, `qdrant_api_key`).

### After

```
add-in ──HTTPS──► /v1/vectors/search ──┐
                                        ├──► SQLAlchemy async session ──► Postgres + pgvector
add-in ──HTTPS──► /v1/vectors/upsert ──┘
```

No new container, no new connection pool. Same `asyncpg` session that
`request_log`, `users`, etc. already use.

### Route naming

Two options:

1. **Keep the path** `/v1/qdrant/*` so the add-in needs no change. Internal module
   gets renamed (`upstream/qdrant.py` → `upstream/pgvector.py`) but the wire path
   stays. **Recommended for the trial.**
2. **Rename** to `/v1/vectors/*` and ship a coordinated add-in update. Cleaner long
   term but blocks on add-in cutover.

Plan below assumes **option 1**. If we pick option 2, add a 308 redirect from the
old path for one release.

## 4. Schema

New extension + one table. Single Alembic revision `0009_pgvector_embeddings`.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE embeddings (
    id           BIGSERIAL PRIMARY KEY,
    collection   TEXT        NOT NULL,
    point_id     TEXT        NOT NULL,
    embedding    vector(1536) NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection, point_id)
);

CREATE INDEX embeddings_collection_idx
    ON embeddings (collection);

CREATE INDEX embeddings_payload_gin
    ON embeddings USING gin (payload jsonb_path_ops);

CREATE INDEX embeddings_hnsw_cos
    ON embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Schema notes

- **Dimension is fixed at table-create time.** 1536 covers OpenAI `text-embedding-3-small`,
  Voyage `voyage-3-lite`, and most current production choices. If we need multiple
  dimensions later, we either bump to `vector(3072)` and pad, or add a second table.
  Document the chosen dim in `GatewayServerPlan.md` §16.
- **`collection`** is a free-form TEXT validated by the existing
  `^[A-Za-z0-9_\-]{1,64}$` regex from `upstream/qdrant.py::validate_collection`.
  We do not break collections out into a separate table — the regex + index is enough
  for trial-month scale.
- **`point_id`** is TEXT (not UUID) because Qdrant supports both numeric and string
  IDs. Upsert key is `(collection, point_id)`.
- **HNSW with cosine.** Matches the default distance Qdrant Cloud uses for
  text-embedding-3-small. If a collection was created with dot-product in Qdrant,
  add a parallel index using `vector_ip_ops` and pick at query time. (Skip until
  needed.)
- **No multi-tenancy column.** Today the gateway treats vector storage as a shared
  resource — same as Qdrant Cloud collections are shared. If we later want
  per-user collections, add `user_id BIGINT REFERENCES users(id)` and a partial
  index.

### Migration file outline

`app/migrations/versions/0009_pgvector_embeddings.py`:

```python
def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "embeddings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("collection", sa.Text, nullable=False),
        sa.Column("point_id", sa.Text, nullable=False),
        # pgvector type via sqlalchemy-pgvector package
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("collection", "point_id", name="uq_embeddings_collection_point"),
    )
    op.create_index("embeddings_collection_idx", "embeddings", ["collection"])
    op.create_index(
        "embeddings_payload_gin",
        "embeddings",
        ["payload"],
        postgresql_using="gin",
        postgresql_ops={"payload": "jsonb_path_ops"},
    )
    op.execute(
        "CREATE INDEX embeddings_hnsw_cos ON embeddings "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

def downgrade() -> None:
    op.drop_index("embeddings_hnsw_cos", table_name="embeddings")
    op.drop_index("embeddings_payload_gin", table_name="embeddings")
    op.drop_index("embeddings_collection_idx", table_name="embeddings")
    op.drop_table("embeddings")
    # Don't drop the extension — other tables/queries may rely on it.
```

Inspect the autogen diff before applying — Alembic does not understand
`Vector(...)` natively without `pgvector.sqlalchemy` registered.

## 5. Dependencies

### Python package

Add to `app/pyproject.toml`:

```
pgvector = "^0.3"
```

`pgvector.sqlalchemy.Vector` provides the SQLAlchemy column type, and
`pgvector.asyncpg.register_vector` wires the wire-format codec into the existing
`asyncpg` connection. **Pure-Python package** — no native compile, no extra apt
deps. The app `Dockerfile` needs **no changes**; the current builder stage
covers it.

### Postgres image

Stock `postgres:16` **does not ship pgvector** — the extension is third-party
and not in core contrib. Swap the image to the official pgvector build, which
is upstream `postgres:16` with the extension preinstalled:

```yaml
# docker-compose.yml
postgres:
  image: pgvector/pgvector:pg16   # was: postgres:16
```

Everything else stays — same `pgdata` volume, same env vars, same `command:`
tuning flags, same healthcheck. Same Postgres 16 binary and on-disk format, so
existing data is read as-is (see §10 for the safe-swap steps).

Verify the extension is available after the image swap (one-time check, before
writing the migration):

```powershell
docker compose exec postgres psql -U gateway -d gateway -c \
  "SELECT * FROM pg_available_extensions WHERE name='vector';"
```

A row in the result means `CREATE EXTENSION vector;` in `0009` will succeed.

## 6. Code changes

| File | Change |
|---|---|
| `app/src/gateway/db/models.py` | Add `Embedding` ORM model (collection, point_id, embedding, payload, timestamps). |
| `app/src/gateway/upstream/qdrant.py` | Replace with `upstream/pgvector.py`. Keep `validate_collection` (move verbatim). Drop `build_client`, `auth_headers`, `search_url`, `upsert_url`. Add `search(session, collection, vector, limit, filter, with_payload, score_threshold)` and `upsert(session, collection, points)` that emit SQL. |
| `app/src/gateway/routes/qdrant.py` | Keep route paths and request models unchanged. Replace the `_proxy` body: no httpx call, no `app.state.qdrant_client`, no `qdrant_api_key`. Logging shape (`endpoint_name="qdrant.search"`, `cost_usd=0`) stays so dashboard reports keep working. |
| `app/src/gateway/main.py` | Remove `app.state.qdrant_client` creation + `aclose()` in lifespan. |
| `app/src/gateway/credential_store.py` | Remove `SETTING_QDRANT_URL`, `SETTING_QDRANT_KEY` from `_ALL_KEYS`. Existing `gateway_settings` rows can stay or be cleaned via a follow-up migration. |
| `app/src/gateway/config.py` | Remove `qdrant_url`, `qdrant_api_key` settings. |
| `app/src/gateway/dashboard/templates/settings.html` | Drop the Qdrant URL/key form fields. |
| `app/src/gateway/dashboard/routes.py` | Drop the Qdrant settings form handlers. |
| `app/src/gateway/cli.py` | Remove any `gateway-admin` subcommands that touch Qdrant settings (if present). |
| `.env.example` | Remove `QDRANT_URL`, `QDRANT_API_KEY`. |
| `docker-compose.yml` | Swap postgres image from `postgres:16` to `pgvector/pgvector:pg16`. Same env vars, same `pgdata` volume, same `command:` flags — no data migration needed (same PG 16 on-disk format). |
| `app/Dockerfile` | **No changes.** `pgvector` is pure Python; the existing builder stage covers it. |
| `GatewayServerPlan.md` | Update §1, §10, §16 to reflect pgvector. |
| `CLAUDE.md` | Update the stack line, drop the Qdrant reference. |

### Filter translation

Qdrant's filter DSL is the one piece that doesn't have a 1:1 SQL equivalent. The
add-in's `SearchGeoswmmDocsTool` only uses simple `must`/`must_not` over payload
fields. Implement a small translator:

```
must:       [{"key": "doc_type", "match": {"value": "manual"}}]
   →        payload @> '{"doc_type": "manual"}'
must:       [{"key": "year",    "range": {"gte": 2020}}]
   →        (payload->>'year')::int >= 2020
```

Anything we don't recognize: return `400 invalid_filter` so we don't silently
ignore a constraint. Document the supported subset in the route docstring.

### Search query shape

```python
stmt = (
    select(Embedding.point_id, Embedding.payload,
           (1 - Embedding.embedding.cosine_distance(query_vec)).label("score"))
    .where(Embedding.collection == collection)
    .where(*filter_clauses)
    .order_by(Embedding.embedding.cosine_distance(query_vec))
    .limit(limit)
)
if score_threshold is not None:
    stmt = stmt.having(text("score >= :t").bindparams(t=score_threshold))
```

Set `SET LOCAL hnsw.ef_search = 64;` at the start of each search transaction for
recall tuning. Expose as `PGVECTOR_EF_SEARCH` in settings if we need to bump it.

### Upsert query shape

```python
stmt = (
    insert(Embedding)
    .values(rows)
    .on_conflict_do_update(
        index_elements=["collection", "point_id"],
        set_={"embedding": insert(Embedding).excluded.embedding,
              "payload": insert(Embedding).excluded.payload,
              "updated_at": func.now()},
    )
)
await session.execute(stmt)
await session.commit()
```

Cap rows per call at 512 (already enforced by the Pydantic model in `routes/qdrant.py`).

## 7. Data backfill

For each existing Qdrant collection we want to keep:

1. Scroll the collection with `POST /collections/{name}/points/scroll` (Qdrant
   REST), batches of 256.
2. Stream rows into `embeddings` using `COPY embeddings (collection, point_id,
   embedding, payload) FROM STDIN` via `asyncpg` — much faster than per-row INSERT
   for the HNSW build.
3. After the load, `REINDEX INDEX CONCURRENTLY embeddings_hnsw_cos;` is *not*
   necessary because HNSW indexes are built incrementally; but a final
   `ANALYZE embeddings;` is.
4. Verify counts: `SELECT collection, count(*) FROM embeddings GROUP BY 1`
   matches the Qdrant `collection.points_count`.

Ship the backfill as a one-shot `gateway-admin migrate-qdrant <collection>` CLI
subcommand in `cli.py`, callable from `docker compose run --rm app`. It reads
Qdrant credentials directly from env (not from `CredentialStore`, since that's
about to be torn out).

## 8. Rollout

Phase the change so we never have a window where the add-in's vector traffic 502s.

| Step | Action | Reversible? |
|---|---|---|
| 1 | Add `pgvector` dep + `0009` migration. Apply to dev DB. | Yes (downgrade) |
| 2 | Implement `Embedding` model + `upstream/pgvector.py` + new route handlers behind a feature flag (`PGVECTOR_ENABLED=false` by default). Both Qdrant Cloud and pgvector implementations live side-by-side, route selects based on flag. | Yes (flip flag) |
| 3 | Run backfill against prod Qdrant → prod Postgres while flag is still `false`. | Yes (truncate `embeddings`) |
| 4 | Flip `PGVECTOR_ENABLED=true` on the VPS. Watch `request_log` for `endpoint='qdrant.search'` errors and latency; compare against a 24h baseline. | Yes (flip flag back) |
| 5 | After 7 days clean, delete Qdrant client code, `qdrant_*` settings, Qdrant Cloud account. | No |

The feature flag adds ~50 lines of branching code. Worth it to keep the rollback
cheap.

## 9. Tests

- **Unit (in-process):** filter translator round-trips, collection regex still
  enforced, vector-length validation, cosine distance math sanity (`a·a == 0`
  distance, `score == 1.0`).
- **Integration (real Postgres in Compose, mirrors `app/tests/test_qdrant.py`):**
  - Insert 100 points → search returns top-K in expected order.
  - Upsert same `point_id` twice → payload updated, no duplicate row.
  - Search with `score_threshold` filters correctly.
  - Search across `must` and `range` filter clauses.
  - Auth/rate-limit/cap interactions identical to today (re-run the existing
    `test_qdrant.py` assertions against the new backend).
- **Performance smoke:** load 10k random 1536-dim vectors, assert p50 search
  latency < 50ms with `limit=10` on the dev container. (Documented baseline so
  future regressions are visible.)

`app/tests/test_qdrant.py` keeps its filename and most of its body — we just
swap the `respx` mock setup for `pytest-asyncio` fixtures that seed the
`embeddings` table.

## 10. Operational

### Image swap (safe — existing data preserved)

Switching from `postgres:16` to `pgvector/pgvector:pg16` is a non-event for
existing data. Both images are the same Postgres 16 binary with the same
on-disk format; the `pgdata` Docker volume mounts cleanly into either. Only
realistic risks are operator error — `docker compose down -v` (deletes the
volume), removing the `volumes:` mount by accident, or picking the wrong major
tag (`pg17` would refuse to start, but doesn't corrupt).

Recommended safe-swap procedure:

```powershell
# 1. Backup first — cheap insurance for trial-size data
docker compose exec postgres pg_dump -U gateway gateway `
  > backup_$(Get-Date -Format yyyyMMdd_HHmm).sql

# 2. Graceful stop (not kill — lets WAL flush cleanly)
docker compose stop postgres

# 3. Edit docker-compose.yml:
#      image: postgres:16  →  image: pgvector/pgvector:pg16
#    Leave everything else (volumes, env, command:, healthcheck) unchanged.

# 4. Start with the new image
docker compose up -d postgres

# 5. Verify existing rows are intact and extension is available
docker compose exec postgres psql -U gateway -d gateway -c "SELECT count(*) FROM users;"
docker compose exec postgres psql -U gateway -d gateway -c "SELECT count(*) FROM request_log;"
docker compose exec postgres psql -U gateway -d gateway -c "SELECT * FROM pg_available_extensions WHERE name='vector';"
```

The extension is **available** after the image swap but not **created** until
the `0009` migration runs `CREATE EXTENSION vector;`. There is no half-state
window — schemas are unaffected until the migration step.

### Other operational notes

- **Backups:** no change needed. `pg_dump` covers `embeddings` + the extension
  metadata automatically. Verify restore on a scratch container before
  declaring P7 done.
- **Index maintenance:** none for trial scale. If we ever hit 1M+ points,
  schedule a nightly `VACUUM ANALYZE embeddings;` via cron.
- **Memory:** HNSW with `m=16` on 1536-dim vectors uses roughly 6.5 KB per
  point. 100k points ≈ 650 MB resident — fine on the VPS's current Postgres
  shared_buffers. Document a "stop here and reconsider" threshold of ~2M
  points (≈13 GB) at which we'd want to pin `maintenance_work_mem` or split
  collections out.

## 11. Risks & open questions

- **Dimension lock-in.** Once we pick `vector(1536)`, every collection must
  match. If we later need a 3072-dim model (e.g. text-embedding-3-large), we
  ship a second table. **Mitigation:** lock dimension at the embeddings
  provider decision in §16 before this migration lands.
- **Recall vs. Qdrant.** HNSW with `m=16, ef_search=64` typically lands at
  ~98% recall@10 vs. Qdrant's defaults. Run a recall A/B during step 4 of
  rollout against a fixed query set.
- **Filter DSL gaps.** If the add-in adds a filter shape we don't translate,
  we return 400. Acceptable for trial, but capture every 400 from the
  translator in `request_log.meta` so we know what to add next.
- **Concurrent index build.** HNSW build is single-threaded per index. On
  large backfills (>500k vectors) the migration step can take tens of
  minutes — build the index **after** the COPY, not before, and run it
  during the rollout's step 3 window so prod traffic never blocks on it.
- **Removing `qdrant_*` settings.** A separate migration `0010_drop_qdrant_settings`
  can clean the `gateway_settings` rows once we're confident in pgvector.
  Don't bundle with `0009` — keep the rollback for step 4 simple.

## 12. Definition of done

- `0009_pgvector_embeddings` applied on dev + staging + prod.
- All `qdrant_*` env vars removed from `.env.example` and the VPS.
- `upstream/qdrant.py` and Qdrant client wiring deleted.
- 7 days of `endpoint='qdrant.search'` traffic in prod with `error_code IS NULL`
  for ≥99.5% of rows.
- `pg_dump` of prod includes `embeddings` and restores cleanly to a scratch
  container.
- `GatewayServerPlan.md` §1, §10, §16 and `CLAUDE.md` stack section updated.
