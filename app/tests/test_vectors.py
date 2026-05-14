"""Integration tests for ``POST /v1/vectors/search`` and ``/v1/vectors/upsert``.

Real Postgres via the ``db_engine`` fixture, full e2e through the FastAPI app
via the ``auth_client`` fixture. The pgvector backend is the only backend.

Cost is fixed at $0.00, so cost-related assertions check
``Decimal("0.000000")`` rather than computing.
"""

from __future__ import annotations

import random
import time
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from gateway.db.models import Embedding, RequestLog, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COLLECTION = "test_docs"
_DIM = 1536  # fixed table dimension


def _unit_vec(index: int, dim: int = _DIM) -> list[float]:
    """Return a unit vector with a 1.0 in position ``index``, 0 elsewhere."""
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


def _seeded_vec(seed: int, dim: int = _DIM) -> list[float]:
    """Return a deterministic pseudo-random unit vector for ``seed``."""
    import math

    rng = random.Random(seed)
    raw = [rng.gauss(0, 1) for _ in range(dim)]
    mag = math.sqrt(sum(x * x for x in raw))
    if mag < 1e-12:
        raw[0] = 1.0
        mag = 1.0
    return [x / mag for x in raw]


async def seed_embeddings(
    session: AsyncSession,
    collection: str,
    n: int,
    seed: int = 42,
) -> list[str]:
    """Insert ``n`` deterministic embeddings into ``collection``.

    Returns the list of ``point_id`` strings in insertion order.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rows = []
    point_ids: list[str] = []
    for i in range(n):
        pid = f"seed-{seed}-{i}"
        point_ids.append(pid)
        rows.append(
            {
                "collection": collection,
                "point_id": pid,
                "embedding": _seeded_vec(seed + i),
                "payload": {"seed_idx": i, "seed": seed},
            }
        )

    stmt = pg_insert(Embedding).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["collection", "point_id"],
        set_={
            "embedding": stmt.excluded.embedding,
            "payload": stmt.excluded.payload,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.commit()
    return point_ids


@pytest.fixture
async def pgvector_session(db_engine: AsyncEngine) -> AsyncSession:
    """Yield an async session for seeding and asserting on the embeddings table."""
    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session


async def _get_request_log_rows(db_engine: AsyncEngine) -> list[RequestLog]:
    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        return (await session.execute(select(RequestLog))).scalars().all()


async def _cleanup_embeddings(db_engine: AsyncEngine, collection: str) -> None:
    """Delete all rows for a collection (between-test cleanup for embeddings)."""
    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM embeddings WHERE collection = :c"),
            {"c": collection},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


async def test_vector_search_unauthenticated_returns_401(
    db_client: AsyncClient,
) -> None:
    resp = await db_client.post(
        "/v1/vectors/search",
        json={"collection": "docs", "vector": [0.1, 0.2], "limit": 3},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "bad_name",
    [
        "../../etc",
        "name with space",
        "",
        "x" * 65,  # too long
        "name/with/slash",
        "name;drop_table",
        "$weird",
    ],
)
async def test_vector_search_invalid_collection_returns_400(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    bad_name: str,
) -> None:
    client, headers, _ = auth_client
    resp = await client.post(
        "/v1/vectors/search",
        headers=headers,
        json={"collection": bad_name, "vector": [0.1], "limit": 3},
    )
    # Pydantic min_length=1 on collection still produces a 422 for empty
    # string — both signal "rejected before touching the backend".
    assert resp.status_code in (400, 422)
    if resp.status_code == 400:
        assert resp.json()["detail"]["error"] == "invalid_collection_name"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_vector_search_writes_request_log_row(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        await seed_embeddings(pgvector_session, _COLLECTION, n=3, seed=11)

        client, headers, user_info = auth_client
        resp = await client.post(
            "/v1/vectors/search",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "vector": _seeded_vec(11),
                "limit": 3,
            },
        )
        assert resp.status_code == 200, resp.text

        rows = await _get_request_log_rows(db_engine)
        assert len(rows) == 1
        row = rows[0]
        assert row.endpoint == "vectors.search"
        assert row.model is None
        assert str(row.user_id) == user_info["id"]
        assert row.status_code == 200
        assert row.cost_usd == Decimal("0.000000")
        assert row.tokens_in == 0
        assert row.tokens_out == 0
        assert row.meta == {"collection": _COLLECTION, "limit": 3}
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


async def test_vector_upsert_writes_request_log_row(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        client, headers, user_info = auth_client
        resp = await client.post(
            "/v1/vectors/upsert",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "points": [
                    {"id": f"u-{i}", "vector": _unit_vec(i), "payload": {}}
                    for i in range(3)
                ],
            },
        )
        assert resp.status_code == 200, resp.text

        rows = await _get_request_log_rows(db_engine)
        assert len(rows) == 1
        row = rows[0]
        assert row.endpoint == "vectors.upsert"
        assert str(row.user_id) == user_info["id"]
        assert row.status_code == 200
        assert row.cost_usd == Decimal("0.000000")
        assert row.meta == {"collection": _COLLECTION, "point_count": 3}
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


# ---------------------------------------------------------------------------
# Monthly cap
# ---------------------------------------------------------------------------


async def test_vector_respects_monthly_cap(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """Cap=0.01, push spend over → next /v1/vectors/search → 402."""
    client, headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(monthly_usd_cap=Decimal("0.01"))
        )
        session.add(
            RequestLog(
                request_id=uuid4(),
                user_id=user_id,
                endpoint="messages",
                model="anthropic/claude-haiku-4.5",
                tokens_in=100,
                tokens_out=50,
                cost_usd=Decimal("0.050000"),
                status_code=200,
                latency_ms=100,
                request_body={"x": 1},
                response_body="ok",
                request_bytes=10,
                response_bytes=2,
            )
        )
        await session.commit()

    resp = await client.post(
        "/v1/vectors/search",
        headers=headers,
        json={"collection": "docs", "vector": [0.1] * _DIM, "limit": 3},
    )
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "monthly_cap_exceeded"


# ---------------------------------------------------------------------------
# 100-point insert → search returns top-K in expected order
# ---------------------------------------------------------------------------


async def test_pgvector_100_point_insert_search_returns_topk_in_order(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Insert 100 seeded vectors, search with a query that should exactly
    match seed #5's vector → expect seed #5 as the top result (score ~1.0)
    and verify descending score ordering across all returned results.
    """
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        await seed_embeddings(pgvector_session, _COLLECTION, n=100, seed=1000)

        query_vec = _seeded_vec(1000 + 5)

        client, headers, _ = auth_client
        resp = await client.post(
            "/v1/vectors/search",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "vector": query_vec,
                "limit": 10,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok"
        results = data["result"]
        assert len(results) == 10

        top = results[0]
        assert top["id"] == "seed-1000-5"
        assert top["score"] > 0.9999

        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True), "Results not in descending score order"
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


# ---------------------------------------------------------------------------
# Upsert same point_id twice → payload updated, no duplicate row
# ---------------------------------------------------------------------------


async def test_pgvector_upsert_same_point_id_twice_updates_payload_no_duplicate(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """INSERT ... ON CONFLICT DO UPDATE must update payload, not add a row."""
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        client, headers, _ = auth_client
        point_id = "upsert-test-1"
        vec = _unit_vec(0)

        resp = await client.post(
            "/v1/vectors/upsert",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "points": [{"id": point_id, "vector": vec, "payload": {"v": 1}}],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "completed"

        resp = await client.post(
            "/v1/vectors/upsert",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "points": [{"id": point_id, "vector": vec, "payload": {"v": 2}}],
            },
        )
        assert resp.status_code == 200, resp.text

        SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(Embedding).where(
                        Embedding.collection == _COLLECTION,
                        Embedding.point_id == point_id,
                    )
                )
            ).scalars().all()
        assert len(rows) == 1, f"Expected 1 row, found {len(rows)}"
        assert rows[0].payload == {"v": 2}, f"Payload was {rows[0].payload!r}"
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


# ---------------------------------------------------------------------------
# score_threshold filtering
# ---------------------------------------------------------------------------


async def test_pgvector_score_threshold_filters_low_score_results(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Insert 3 orthogonal unit vectors; query along axis 0.

    Only axis-0 vector has score 1.0; the other two are orthogonal (score 0.0).
    A score_threshold of 0.5 should return only the axis-0 result.
    """
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        rows = [
            {
                "collection": _COLLECTION,
                "point_id": f"orth-{i}",
                "embedding": _unit_vec(i),
                "payload": {"axis": i},
            }
            for i in range(3)
        ]
        stmt = pg_insert(Embedding).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["collection", "point_id"],
            set_={
                "embedding": stmt.excluded.embedding,
                "payload": stmt.excluded.payload,
                "updated_at": func.now(),
            },
        )
        await pgvector_session.execute(stmt)
        await pgvector_session.commit()

        client, headers, _ = auth_client
        resp = await client.post(
            "/v1/vectors/search",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "vector": _unit_vec(0),
                "limit": 10,
                "score_threshold": 0.5,
            },
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["result"]
        assert len(results) == 1, f"Expected 1 result, got {len(results)}: {results}"
        assert results[0]["id"] == "orth-0"
        assert results[0]["score"] > 0.999
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


# ---------------------------------------------------------------------------
# Filter DSL: must with match.value and must with range.gte
# ---------------------------------------------------------------------------


async def test_pgvector_filter_must_match_value_returns_expected_subset(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Insert two points with different doc_type values; filter on one."""
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        rows = [
            {
                "collection": _COLLECTION,
                "point_id": "match-manual",
                "embedding": _unit_vec(0),
                "payload": {"doc_type": "manual"},
            },
            {
                "collection": _COLLECTION,
                "point_id": "match-guide",
                "embedding": _unit_vec(1),
                "payload": {"doc_type": "guide"},
            },
        ]
        stmt = pg_insert(Embedding).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["collection", "point_id"],
            set_={
                "embedding": stmt.excluded.embedding,
                "payload": stmt.excluded.payload,
                "updated_at": func.now(),
            },
        )
        await pgvector_session.execute(stmt)
        await pgvector_session.commit()

        client, headers, _ = auth_client
        resp = await client.post(
            "/v1/vectors/search",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "vector": _unit_vec(0),
                "limit": 10,
                "filter": {"must": [{"key": "doc_type", "match": {"value": "manual"}}]},
            },
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["result"]
        assert len(results) == 1, f"Expected 1 result, got {results}"
        assert results[0]["id"] == "match-manual"
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


async def test_pgvector_filter_must_range_gte_returns_expected_subset(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Insert two points with different year values; filter with range.gte."""
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        rows = [
            {
                "collection": _COLLECTION,
                "point_id": "year-2019",
                "embedding": _unit_vec(0),
                "payload": {"year": 2019},
            },
            {
                "collection": _COLLECTION,
                "point_id": "year-2022",
                "embedding": _unit_vec(1),
                "payload": {"year": 2022},
            },
        ]
        stmt = pg_insert(Embedding).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["collection", "point_id"],
            set_={
                "embedding": stmt.excluded.embedding,
                "payload": stmt.excluded.payload,
                "updated_at": func.now(),
            },
        )
        await pgvector_session.execute(stmt)
        await pgvector_session.commit()

        client, headers, _ = auth_client
        resp = await client.post(
            "/v1/vectors/search",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "vector": _unit_vec(1),
                "limit": 10,
                "filter": {"must": [{"key": "year", "range": {"gte": 2020}}]},
            },
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["result"]
        assert len(results) == 1, f"Expected 1 result, got {results}"
        assert results[0]["id"] == "year-2022"
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


# ---------------------------------------------------------------------------
# Invalid filter → 400 invalid_filter
# ---------------------------------------------------------------------------


async def test_pgvector_invalid_filter_returns_400(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
) -> None:
    """An unsupported filter shape returns 400."""
    client, headers, _ = auth_client
    resp = await client.post(
        "/v1/vectors/search",
        headers=headers,
        json={
            "collection": "docs",
            "vector": [0.1] * _DIM,
            "limit": 5,
            "filter": {"should": [{"key": "x", "match": {"value": 1}}]},
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_filter"


# ---------------------------------------------------------------------------
# request_log.meta shape
# ---------------------------------------------------------------------------


async def test_pgvector_search_request_log_meta(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """``request_log`` row for vector search carries collection + limit."""
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        await seed_embeddings(pgvector_session, _COLLECTION, n=5, seed=7)

        client, headers, _user_info = auth_client
        resp = await client.post(
            "/v1/vectors/search",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "vector": _seeded_vec(7),
                "limit": 3,
            },
        )
        assert resp.status_code == 200, resp.text

        rows = await _get_request_log_rows(db_engine)
        assert len(rows) == 1
        row = rows[0]
        assert row.endpoint == "vectors.search"
        assert row.cost_usd == Decimal("0.000000")
        assert row.meta is not None
        assert row.meta.get("collection") == _COLLECTION
        assert row.meta.get("limit") == 3
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


async def test_pgvector_upsert_request_log_meta(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """``request_log`` row for vector upsert carries collection + point_count."""
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        client, headers, _user_info = auth_client
        resp = await client.post(
            "/v1/vectors/upsert",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "points": [{"id": "log-test-1", "vector": _unit_vec(0), "payload": {}}],
            },
        )
        assert resp.status_code == 200, resp.text

        rows = await _get_request_log_rows(db_engine)
        assert len(rows) == 1
        row = rows[0]
        assert row.endpoint == "vectors.upsert"
        assert row.cost_usd == Decimal("0.000000")
        assert row.meta is not None
        assert row.meta.get("collection") == _COLLECTION
        assert row.meta.get("point_count") == 1
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


# ---------------------------------------------------------------------------
# Performance smoke (opt-in, skipped by default)
# ---------------------------------------------------------------------------
#
# Enable with:
#   pytest -m slow app/tests/test_vectors.py
#
_PERF_COLLECTION = "perf_smoke_10k"


@pytest.mark.slow
async def test_pgvector_10k_vectors_p50_search_latency_under_50ms(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Insert 10k vectors and assert p50 search latency < 50ms."""
    N = 10_000
    BATCH_SIZE = 512
    QUERY_ROUNDS = 20
    P50_TARGET_MS = 50.0

    await _cleanup_embeddings(db_engine, _PERF_COLLECTION)
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        rng = random.Random(99)

        def _rand_vec() -> list[float]:
            import math
            raw = [rng.gauss(0, 1) for _ in range(_DIM)]
            mag = math.sqrt(sum(x * x for x in raw))
            return [x / mag for x in raw]

        for batch_start in range(0, N, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, N)
            rows = [
                {
                    "collection": _PERF_COLLECTION,
                    "point_id": f"perf-{i}",
                    "embedding": _rand_vec(),
                    "payload": {"i": i},
                }
                for i in range(batch_start, batch_end)
            ]
            stmt = pg_insert(Embedding).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["collection", "point_id"],
                set_={
                    "embedding": stmt.excluded.embedding,
                    "payload": stmt.excluded.payload,
                    "updated_at": func.now(),
                },
            )
            await pgvector_session.execute(stmt)
            await pgvector_session.commit()

        client, headers, _ = auth_client
        latencies_ms: list[float] = []

        for _ in range(QUERY_ROUNDS):
            q = _rand_vec()
            t0 = time.monotonic()
            resp = await client.post(
                "/v1/vectors/search",
                headers=headers,
                json={"collection": _PERF_COLLECTION, "vector": q, "limit": 10},
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            assert resp.status_code == 200, resp.text
            latencies_ms.append(elapsed_ms)

        latencies_ms.sort()
        p50 = latencies_ms[QUERY_ROUNDS // 2]
        assert p50 < P50_TARGET_MS, (
            f"p50 latency {p50:.1f}ms exceeds {P50_TARGET_MS}ms target. "
            f"All latencies: {[f'{x:.1f}' for x in latencies_ms]}"
        )
    finally:
        await _cleanup_embeddings(db_engine, _PERF_COLLECTION)
