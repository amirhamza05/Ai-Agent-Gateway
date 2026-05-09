"""Integration tests for ``POST /v1/embeddings``.

Same conventions as ``test_messages_stream`` — real Postgres, mocked
OpenRouter via ``respx``, full e2e through the FastAPI app via the
``auth_client`` fixture. Skipped wholesale when ``TEST_DATABASE_URL``
isn't set (the ``db_engine`` fixture handles that).

The embeddings endpoint is non-streaming so the assertions are simpler
than the messages tests: status code, JSON body, request_log row.
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


# ---- Helpers --------------------------------------------------------------


def _embeddings_payload(
    *,
    tokens: int = 10,
    dim: int = 4,
    n_inputs: int = 2,
) -> dict[str, Any]:
    """Build an OpenAI-shape embeddings response body for the mock."""
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": [0.1] * dim, "index": i}
            for i in range(n_inputs)
        ],
        "model": "openai/text-embedding-3-small",
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


# ---- Auth + validation ----------------------------------------------------


async def test_embeddings_unauthenticated_returns_401(db_client: AsyncClient) -> None:
    resp = await db_client.post(
        "/v1/embeddings",
        json={
            "model": "openai/text-embedding-3-small",
            "input": ["hello"],
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "unauthorized"}


async def test_embeddings_disallowed_model_returns_400(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
) -> None:
    client, headers, _ = auth_client
    resp = await client.post(
        "/v1/embeddings",
        headers=headers,
        json={
            "model": "voyage/voyage-3",
            "input": ["hello"],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "model_not_allowed"


async def test_embeddings_input_too_large_returns_422(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
) -> None:
    """More than 512 strings rejected by the Pydantic max_length."""
    client, headers, _ = auth_client
    resp = await client.post(
        "/v1/embeddings",
        headers=headers,
        json={
            "model": "openai/text-embedding-3-small",
            "input": ["x"] * 513,
        },
    )
    assert resp.status_code == 422


async def test_embeddings_input_empty_returns_422(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
) -> None:
    """Empty input array rejected — would cost a no-op upstream call."""
    client, headers, _ = auth_client
    resp = await client.post(
        "/v1/embeddings",
        headers=headers,
        json={
            "model": "openai/text-embedding-3-small",
            "input": [],
        },
    )
    assert resp.status_code == 422


# ---- Happy path ----------------------------------------------------------


async def test_embeddings_happy_path_returns_passthrough(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
) -> None:
    client, headers, _ = auth_client

    upstream_body = _embeddings_payload(tokens=12, dim=8, n_inputs=2)
    openrouter_mock.post("/embeddings").mock(
        return_value=httpx.Response(200, json=upstream_body)
    )

    resp = await client.post(
        "/v1/embeddings",
        headers=headers,
        json={
            "model": "openai/text-embedding-3-small",
            "input": ["hello", "world"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Pass-through unchanged — the add-in's OpenAI-shape parser keeps
    # working with no extra translation.
    assert body == upstream_body
    assert resp.headers.get("x-request-id")


async def test_embeddings_writes_request_log_row(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    client, headers, user_info = auth_client

    upstream_body = _embeddings_payload(tokens=42, dim=4, n_inputs=2)
    openrouter_mock.post("/embeddings").mock(
        return_value=httpx.Response(200, json=upstream_body)
    )

    resp = await client.post(
        "/v1/embeddings",
        headers=headers,
        json={
            "model": "openai/text-embedding-3-small",
            "input": ["hello", "world"],
        },
    )
    assert resp.status_code == 200, resp.text

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        rows = (await session.execute(select(RequestLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "embeddings"
    assert str(row.user_id) == user_info["id"]
    assert row.status_code == 200
    assert row.model == "openai/text-embedding-3-small"
    assert row.tokens_in == 42
    assert row.tokens_out == 0
    # 42 tokens × $0.020 / 1e6 = 0.00000084 → quantized to 6 decimals = 0.000001.
    assert row.cost_usd is not None
    assert row.cost_usd == Decimal("0.000001")
    assert row.request_body is not None
    assert row.request_body["model"] == "openai/text-embedding-3-small"
    assert row.request_body["input"] == ["hello", "world"]
    assert row.response_body is not None
    assert row.request_bytes and row.request_bytes > 0
    assert row.response_bytes and row.response_bytes > 0
    assert row.meta == {"input_count": 2}


async def test_embeddings_upstream_5xx_recorded_as_error(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    client, headers, _ = auth_client

    err_body = {"error": {"message": "provider blew up", "code": "provider_error"}}
    openrouter_mock.post("/embeddings").mock(
        return_value=httpx.Response(500, json=err_body)
    )

    resp = await client.post(
        "/v1/embeddings",
        headers=headers,
        json={
            "model": "openai/text-embedding-3-small",
            "input": ["hello"],
        },
    )
    # Gateway forwards the upstream status verbatim so the add-in can
    # surface "provider error" rather than misattribute as gateway 502.
    assert resp.status_code == 500
    assert resp.json() == err_body

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        row = (await session.execute(select(RequestLog))).scalar_one()
    assert row.endpoint == "embeddings"
    assert row.status_code == 500
    assert row.error_code == "upstream_500"
    # No usage on error → tokens_in / cost_usd remain at the defaults
    # (0 / Decimal("0")) rather than NULL — the row is still a valid
    # ledger entry, just with zero attribution.
    assert row.tokens_in == 0


async def test_embeddings_respects_monthly_cap(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """Cap=0.01, push spend over → next /v1/embeddings → 402."""
    client, headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(monthly_usd_cap=Decimal("0.01"))
        )
        # One row with $0.05 — past the $0.01 cap.
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
        "/v1/embeddings",
        headers=headers,
        json={
            "model": "openai/text-embedding-3-small",
            "input": ["hello"],
        },
    )
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "monthly_cap_exceeded"
    assert detail["spent_usd"] == pytest.approx(0.05, abs=1e-9)
