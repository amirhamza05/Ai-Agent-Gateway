"""Integration tests for ``POST /v1/qdrant/search`` and ``/v1/qdrant/upsert``.

Same conventions as ``test_embeddings`` — real Postgres, mocked Qdrant
via ``respx`` (``qdrant_mock`` fixture), full e2e through the FastAPI
app via ``auth_client``.

Cost is fixed at $0.00 (Qdrant Cloud bills on storage), so cost-related
assertions check ``Decimal("0.000000")`` rather than computing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from gateway.db.models import RequestLog, User


# ---- Search --------------------------------------------------------------


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

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        rows = (await session.execute(select(RequestLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "qdrant.search"
    assert row.model is None
    assert str(row.user_id) == user_info["id"]
    assert row.status_code == 200
    assert row.cost_usd == Decimal("0.000000")
    assert row.tokens_in == 0
    assert row.tokens_out == 0
    assert row.meta == {"collection": "docs", "limit": 7}
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

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        rows = (await session.execute(select(RequestLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "qdrant.upsert"
    assert str(row.user_id) == user_info["id"]
    assert row.status_code == 200
    assert row.cost_usd == Decimal("0.000000")
    assert row.meta == {"collection": "docs", "point_count": 3}


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

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        row = (await session.execute(select(RequestLog))).scalar_one()
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
