"""Integration tests for ``POST /v1/qdrant/search`` and ``/v1/qdrant/upsert``.

Same conventions as ``test_embeddings`` — real Postgres, mocked Qdrant
via ``respx`` (``qdrant_mock`` fixture), full e2e through the FastAPI
app via ``auth_client``.

Cost is fixed at $0.00 (Qdrant Cloud bills on storage), so cost-related
assertions check ``Decimal("0.000000")`` rather than computing.

Backend parametrization
-----------------------
Tests that touch the actual Qdrant/pgvector backend are parametrized with
``@pytest.mark.parametrize("backend", ["qdrant", "pgvector"])``.

* ``backend="qdrant"``  — ``PGVECTOR_ENABLED`` is False; upstream calls are
  intercepted by ``respx`` via the ``qdrant_mock`` fixture.
* ``backend="pgvector"`` — ``PGVECTOR_ENABLED`` is True; the route calls the
  local Postgres pgvector backend directly (real DB, no outbound HTTP).

The fixture ``qdrant_or_pgvector_client`` injects the right app override and
returns a ``(client, headers, user_info)`` triple identical to ``auth_client``
so test bodies can stay backend-agnostic.
"""

from __future__ import annotations

import asyncio
import random
import time
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
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

_AUTH_EMAIL = "qdrant-test@example.com"
_AUTH_PASSWORD = "correcthorsebattery"


def _unit_vec(index: int, dim: int = _DIM) -> list[float]:
    """Return a unit vector with a 1.0 in position ``index``, 0 elsewhere.

    These are perfectly orthogonal, so cosine ordering between them is
    unambiguous: a query along axis ``i`` returns the row seeded on axis ``i``
    with score 1.0, and all others with score 0.0.
    """
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


def _seeded_vec(seed: int, dim: int = _DIM) -> list[float]:
    """Return a deterministic pseudo-random unit vector for ``seed``.

    Uses Python's built-in RNG (seeded, not cryptographic).  The magnitude
    is normalised to 1.0 so cosine comparisons are meaningful.
    """
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

    Uses ``pg_insert(...).on_conflict_do_update`` so the function is safe
    to call in a test that may run more than once against the same DB (e.g.
    when pytest-xdist shares a worker, or when a previous test leaked rows
    because its rollback didn't fire).  The call commits the session so the
    rows are visible to the app's own session.
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


# ---------------------------------------------------------------------------
# Per-backend fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def pgvector_db_app(db_engine: object) -> object:
    """Like ``db_app`` but with ``pgvector_enabled=True`` injected via
    dependency override.

    The lifespan runs normally (Qdrant httpx client is built but never
    reached on the pgvector path).  We swap the session factory and override
    ``get_settings`` to return a Settings copy with ``pgvector_enabled=True``.
    """
    from gateway.config import Settings, get_settings
    from gateway.main import create_app
    from sqlalchemy.ext.asyncio import async_sessionmaker

    app = create_app()
    yield_ctx = app.router.lifespan_context(app)
    await yield_ctx.__aenter__()
    try:
        app.state.db_engine = db_engine
        app.state.db_session_factory = async_sessionmaker(
            bind=db_engine,
            expire_on_commit=False,
        )

        # Build a Settings instance with pgvector_enabled=True.
        # We can't mutate the lru_cache singleton directly — instead we
        # override the FastAPI dependency so only this app instance sees it.
        real_settings = get_settings()
        # Construct a copy with pgvector_enabled flipped.
        overridden = real_settings.model_copy(update={"pgvector_enabled": True})

        app.dependency_overrides[get_settings] = lambda: overridden
        yield app
    finally:
        app.dependency_overrides.pop(get_settings, None)
        await yield_ctx.__aexit__(None, None, None)


@pytest.fixture
async def pgvector_auth_client(
    pgvector_db_app: object,
) -> tuple[AsyncClient, dict[str, str], dict[str, object]]:
    """Register + log in a fresh user on the pgvector-enabled app."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=pgvector_db_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        reg = await ac.post(
            "/auth/register",
            json={"email": _AUTH_EMAIL, "password": _AUTH_PASSWORD},
        )
        assert reg.status_code == 201, reg.text
        user_info = reg.json()

        login = await ac.post(
            "/auth/login",
            json={"email": _AUTH_EMAIL, "password": _AUTH_PASSWORD},
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        yield ac, headers, user_info


@pytest.fixture
async def pgvector_session(db_engine: AsyncEngine) -> AsyncSession:
    """Yield an async session for seeding and asserting on the embeddings table.

    The session is closed (not rolled back) at teardown so that seeded rows
    persist for the duration of the test.  Row cleanup happens via the
    ``db_engine`` fixture which TRUNCATEs the users/request_log tables; the
    ``embeddings`` table is cleaned up by the explicit DELETE in each test
    that needs isolation.
    """
    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Test helpers shared by both backends
# ---------------------------------------------------------------------------


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
# Original Qdrant-path tests (preserved)
# ---------------------------------------------------------------------------


async def test_qdrant_search_unauthenticated_returns_401(
    db_client: AsyncClient,
) -> None:
    resp = await db_client.post(
        "/v1/qdrant/search",
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
        "x" * 65,            # too long
        "name/with/slash",
        "name;drop_table",
        "$weird",
    ],
)
async def test_qdrant_search_invalid_collection_returns_400(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    bad_name: str,
) -> None:
    client, headers, _ = auth_client
    resp = await client.post(
        "/v1/qdrant/search",
        headers=headers,
        json={"collection": bad_name, "vector": [0.1], "limit": 3},
    )
    # Pydantic min_length=1 on collection still produces a 422 for
    # empty string — that's fine, both signal "rejected before
    # touching upstream". For the others we get our 400 with the
    # ``invalid_collection_name`` shape.
    assert resp.status_code in (400, 422)
    if resp.status_code == 400:
        assert resp.json()["detail"]["error"] == "invalid_collection_name"


async def test_qdrant_search_happy_path_returns_passthrough(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
) -> None:
    client, headers, _ = auth_client

    upstream_body = {
        "result": [
            {"id": 1, "score": 0.9, "payload": {"title": "swmm intro"}},
            {"id": 2, "score": 0.8, "payload": {"title": "hydraulics"}},
        ],
        "status": "ok",
        "time": 0.001,
    }
    qdrant_mock.post("/collections/docs/points/search").mock(
        return_value=httpx.Response(200, json=upstream_body)
    )

    resp = await client.post(
        "/v1/qdrant/search",
        headers=headers,
        json={
            "collection": "docs",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "limit": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == upstream_body
    assert resp.headers.get("x-request-id")


async def test_qdrant_search_writes_request_log_row(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    client, headers, user_info = auth_client

    qdrant_mock.post("/collections/docs/points/search").mock(
        return_value=httpx.Response(200, json={"result": [], "status": "ok"})
    )

    resp = await client.post(
        "/v1/qdrant/search",
        headers=headers,
        json={
            "collection": "docs",
            "vector": [0.1, 0.2],
            "limit": 7,
        },
    )
    assert resp.status_code == 200, resp.text

    rows = await _get_request_log_rows(db_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "qdrant.search"
    assert row.model is None
    assert str(row.user_id) == user_info["id"]
    assert row.status_code == 200
    assert row.cost_usd == Decimal("0.000000")
    assert row.tokens_in == 0
    assert row.tokens_out == 0
    assert row.meta == {"collection": "docs", "limit": 7, "backend": "qdrant"}
    assert row.request_body is not None
    assert row.request_body["limit"] == 7


# ---- Upsert --------------------------------------------------------------


async def test_qdrant_upsert_happy_path_returns_passthrough(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
) -> None:
    client, headers, _ = auth_client

    upstream_body = {"result": {"operation_id": 42, "status": "completed"}, "status": "ok"}
    # Route forwards to ``/collections/{c}/points?wait=true`` (PUT). Use
    # a regex so we match regardless of the ``wait`` query param.
    qdrant_mock.put(
        url__regex=r".*/collections/docs/points(\?.*)?$"
    ).mock(
        return_value=httpx.Response(200, json=upstream_body)
    )

    resp = await client.post(
        "/v1/qdrant/upsert",
        headers=headers,
        json={
            "collection": "docs",
            "points": [
                {"id": 1, "vector": [0.1, 0.2], "payload": {"title": "intro"}},
                {"id": 2, "vector": [0.3, 0.4], "payload": {"title": "next"}},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == upstream_body
    assert resp.headers.get("x-request-id")


async def test_qdrant_upsert_writes_request_log_row(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    client, headers, user_info = auth_client

    qdrant_mock.put(
        url__regex=r".*/collections/docs/points(\?.*)?$"
    ).mock(
        return_value=httpx.Response(200, json={"result": {"status": "completed"}})
    )

    resp = await client.post(
        "/v1/qdrant/upsert",
        headers=headers,
        json={
            "collection": "docs",
            "points": [
                {"id": i, "vector": [0.0, 0.1], "payload": {}}
                for i in range(3)
            ],
        },
    )
    assert resp.status_code == 200, resp.text

    rows = await _get_request_log_rows(db_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "qdrant.upsert"
    assert str(row.user_id) == user_info["id"]
    assert row.status_code == 200
    assert row.cost_usd == Decimal("0.000000")
    assert row.meta == {"collection": "docs", "point_count": 3, "backend": "qdrant"}


# ---- Error path ---------------------------------------------------------


async def test_qdrant_upstream_4xx_recorded_as_error(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    """Qdrant returns 404 (collection not found) → gateway forwards verbatim."""
    client, headers, _ = auth_client

    err_body = {"status": {"error": "Collection 'missing' not found"}, "time": 0.0}
    qdrant_mock.post("/collections/missing/points/search").mock(
        return_value=httpx.Response(404, json=err_body)
    )

    resp = await client.post(
        "/v1/qdrant/search",
        headers=headers,
        json={
            "collection": "missing",
            "vector": [0.1, 0.2],
            "limit": 3,
        },
    )
    assert resp.status_code == 404
    assert resp.json() == err_body

    rows = await _get_request_log_rows(db_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "qdrant.search"
    assert row.status_code == 404
    assert row.error_code == "upstream_404"


# ---- Cap -----------------------------------------------------------------


async def test_qdrant_respects_monthly_cap(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """Cap=0.01, push spend over → next /v1/qdrant/search → 402."""
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
        "/v1/qdrant/search",
        headers=headers,
        json={"collection": "docs", "vector": [0.1, 0.2], "limit": 3},
    )
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "monthly_cap_exceeded"


# ---------------------------------------------------------------------------
# validate_collection — rejected on BOTH backends via the shared route layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["qdrant", "pgvector"])
@pytest.mark.parametrize(
    "bad_name",
    [
        "../../etc",
        "name with space",
        "x" * 65,
        "name/with/slash",
        "$weird",
    ],
)
async def test_invalid_collection_name_returns_400_on_both_backends(
    backend: str,
    bad_name: str,
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
) -> None:
    """``validate_collection`` fires before the backend branch — both paths 400."""
    if backend == "qdrant":
        client, headers, _ = auth_client
    else:
        client, headers, _ = pgvector_auth_client

    resp = await client.post(
        "/v1/qdrant/search",
        headers=headers,
        json={"collection": bad_name, "vector": [0.1], "limit": 3},
    )
    assert resp.status_code in (400, 422)
    if resp.status_code == 400:
        assert resp.json()["detail"]["error"] == "invalid_collection_name"


# ---------------------------------------------------------------------------
# 100-point insert → search returns top-K in expected order
# ---------------------------------------------------------------------------


async def test_pgvector_100_point_insert_search_returns_topk_in_order(
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
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

        # Query vector: the same deterministic vector as seed row 5.
        query_vec = _seeded_vec(1000 + 5)

        client, headers, _ = pgvector_auth_client
        resp = await client.post(
            "/v1/qdrant/search",
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

        # Top result must be the exact match — score very close to 1.0.
        top = results[0]
        assert top["id"] == "seed-1000-5"
        assert top["score"] > 0.9999

        # Scores must be in descending order.
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True), "Results not in descending score order"
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


async def test_qdrant_100_point_search_passthrough(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
) -> None:
    """Qdrant path: mock returns deterministic ordered payload → gateway
    passes it through unchanged and we assert the ordering is preserved."""
    client, headers, _ = auth_client

    expected_results = [
        {"id": f"pt-{i}", "score": 1.0 - i * 0.1, "payload": {"rank": i}}
        for i in range(10)
    ]
    qdrant_mock.post(f"/collections/{_COLLECTION}/points/search").mock(
        return_value=httpx.Response(
            200,
            json={"result": expected_results, "status": "ok", "time": 0.001},
        )
    )

    resp = await client.post(
        "/v1/qdrant/search",
        headers=headers,
        json={"collection": _COLLECTION, "vector": [0.1] * 4, "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == expected_results


# ---------------------------------------------------------------------------
# Upsert same point_id twice → payload updated, no duplicate row
# ---------------------------------------------------------------------------


async def test_pgvector_upsert_same_point_id_twice_updates_payload_no_duplicate(
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """INSERT ... ON CONFLICT DO UPDATE must update payload, not add a row."""
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        client, headers, _ = pgvector_auth_client
        point_id = "upsert-test-1"
        vec = _unit_vec(0)

        # First upsert.
        resp = await client.post(
            "/v1/qdrant/upsert",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "points": [{"id": point_id, "vector": vec, "payload": {"v": 1}}],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "completed"

        # Second upsert — different payload.
        resp = await client.post(
            "/v1/qdrant/upsert",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "points": [{"id": point_id, "vector": vec, "payload": {"v": 2}}],
            },
        )
        assert resp.status_code == 200, resp.text

        # Assert exactly one row in the DB and the payload reflects the second write.
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


async def test_qdrant_upsert_passthrough_on_both_calls(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
) -> None:
    """Qdrant path: gateway forwards the second upsert; no DB assertions."""
    client, headers, _ = auth_client

    qdrant_mock.put(
        url__regex=rf".*/collections/{_COLLECTION}/points(\?.*)?$"
    ).mock(
        return_value=httpx.Response(
            200, json={"result": {"operation_id": 1, "status": "completed"}, "status": "ok"}
        )
    )

    for v in [1, 2]:
        resp = await client.post(
            "/v1/qdrant/upsert",
            headers=headers,
            json={
                "collection": _COLLECTION,
                "points": [{"id": "pt-1", "vector": [0.1] * 4, "payload": {"v": v}}],
            },
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# score_threshold filtering
# ---------------------------------------------------------------------------


async def test_pgvector_score_threshold_filters_low_score_results(
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
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

        client, headers, _ = pgvector_auth_client
        resp = await client.post(
            "/v1/qdrant/search",
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
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
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

        client, headers, _ = pgvector_auth_client
        resp = await client.post(
            "/v1/qdrant/search",
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
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
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

        client, headers, _ = pgvector_auth_client
        resp = await client.post(
            "/v1/qdrant/search",
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


async def test_qdrant_filter_is_forwarded_unchanged(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
) -> None:
    """Qdrant path: the filter dict is forwarded verbatim to upstream."""
    client, headers, _ = auth_client
    the_filter = {"must": [{"key": "doc_type", "match": {"value": "manual"}}]}

    captured: list[dict[str, Any]] = []

    def _capture(req: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(req.content)
        captured.append(body)
        return httpx.Response(
            200, json={"result": [], "status": "ok", "time": 0.0}
        )

    qdrant_mock.post("/collections/docs/points/search").mock(side_effect=_capture)

    resp = await client.post(
        "/v1/qdrant/search",
        headers=headers,
        json={"collection": "docs", "vector": [0.1, 0.2], "limit": 5, "filter": the_filter},
    )
    assert resp.status_code == 200, resp.text
    assert len(captured) == 1
    assert captured[0].get("filter") == the_filter


# ---------------------------------------------------------------------------
# Invalid filter → 400 invalid_filter on pgvector path
# ---------------------------------------------------------------------------


async def test_pgvector_invalid_filter_returns_400(
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
) -> None:
    """An unsupported Qdrant filter shape returns 400 on the pgvector path."""
    client, headers, _ = pgvector_auth_client
    resp = await client.post(
        "/v1/qdrant/search",
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
# request_log row — meta.backend set correctly
# ---------------------------------------------------------------------------


async def test_pgvector_search_request_log_row_has_correct_backend_meta(
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """``request_log`` row for pgvector search must have ``meta.backend='pgvector'``."""
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        await seed_embeddings(pgvector_session, _COLLECTION, n=5, seed=7)

        client, headers, user_info = pgvector_auth_client
        resp = await client.post(
            "/v1/qdrant/search",
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
        assert row.endpoint == "qdrant.search"
        assert row.cost_usd == Decimal("0.000000")
        assert row.meta is not None
        assert row.meta.get("backend") == "pgvector"
        assert row.meta.get("collection") == _COLLECTION
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


async def test_pgvector_upsert_request_log_row_has_correct_backend_meta(
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """``request_log`` row for pgvector upsert must have ``meta.backend='pgvector'``."""
    await _cleanup_embeddings(db_engine, _COLLECTION)
    try:
        client, headers, user_info = pgvector_auth_client
        resp = await client.post(
            "/v1/qdrant/upsert",
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
        assert row.endpoint == "qdrant.upsert"
        assert row.cost_usd == Decimal("0.000000")
        assert row.meta is not None
        assert row.meta.get("backend") == "pgvector"
        assert row.meta.get("collection") == _COLLECTION
        assert row.meta.get("point_count") == 1
    finally:
        await _cleanup_embeddings(db_engine, _COLLECTION)


async def test_qdrant_search_request_log_row_has_qdrant_backend_meta(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    """``request_log`` row for Qdrant Cloud search must have ``meta.backend='qdrant'``."""
    client, headers, user_info = auth_client

    qdrant_mock.post("/collections/docs/points/search").mock(
        return_value=httpx.Response(200, json={"result": [], "status": "ok"})
    )

    resp = await client.post(
        "/v1/qdrant/search",
        headers=headers,
        json={"collection": "docs", "vector": [0.1, 0.2], "limit": 5},
    )
    assert resp.status_code == 200, resp.text

    rows = await _get_request_log_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].meta.get("backend") == "qdrant"


# ---------------------------------------------------------------------------
# Auth/cap passes through before the backend branch (shared gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["qdrant", "pgvector"])
async def test_monthly_cap_enforced_before_backend_branch(
    backend: str,
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    qdrant_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    """``enforce_monthly_cap`` fires before the Qdrant/pgvector branch."""
    if backend == "qdrant":
        client, headers, user_info = auth_client
    else:
        client, headers, user_info = pgvector_auth_client

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
                cost_usd=Decimal("0.500000"),
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
        "/v1/qdrant/search",
        headers=headers,
        json={"collection": "docs", "vector": [0.1] * _DIM, "limit": 3},
    )
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["error"] == "monthly_cap_exceeded"


# ---------------------------------------------------------------------------
# Performance smoke (opt-in, skipped by default)
# ---------------------------------------------------------------------------
#
# Enable with:
#   pytest -m slow app/tests/test_qdrant.py
#
# This test inserts 10 000 random 1536-dim vectors into a dedicated collection
# and measures p50 search latency for ``limit=10``.  The p50 target of <50ms
# is based on a dev-container baseline with the HNSW index (m=16,
# ef_construction=64, ef_search=64).  If the container is running without the
# index (e.g. immediately after a TRUNCATE) the first query may be much slower
# because HNSW builds lazily; this test seeds incrementally, so the index
# should be active by query time.
#
# Failures here indicate either a regression in the HNSW configuration or a
# severely under-resourced container.  Don't commit ef_search changes without
# re-running this test and updating the p50 comment.
#
_PERF_COLLECTION = "perf_smoke_10k"


@pytest.mark.slow
async def test_pgvector_10k_vectors_p50_search_latency_under_50ms(
    pgvector_auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    pgvector_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Insert 10k vectors and assert p50 search latency < 50ms.

    Marked ``@pytest.mark.slow`` — excluded from the default suite via
    ``pytest -m 'not slow'``.  Run explicitly with ``pytest -m slow``.

    The p50 target (50ms) was chosen conservatively for a 2-core Docker
    container with 2 GB RAM.  On a modern laptop with the HNSW index warmed
    by the insert phase, observed p50 is typically 1–5ms.  If you upgrade the
    container specs, tighten this to 20ms.
    """
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

        # Bulk-insert in batches.
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

        # Measure search latency.
        client, headers, _ = pgvector_auth_client
        latencies_ms: list[float] = []

        for _ in range(QUERY_ROUNDS):
            q = _rand_vec()
            t0 = time.monotonic()
            resp = await client.post(
                "/v1/qdrant/search",
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
