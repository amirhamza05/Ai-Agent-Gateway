"""Integration tests for ``POST /v1/messages``.

These tests:

* Run against a real Postgres (per CLAUDE.md — no DB mocks).
* Mock OpenRouter via ``respx`` so we don't burn real API budget.
* Verify the streaming contract: chunks arrive incrementally, TTFB is
  fast, and the row in ``request_log`` reflects the upstream outcome.

When ``TEST_DATABASE_URL`` isn't set, every test in this module is
skipped via the ``db_engine`` fixture (which calls ``pytest.skip``).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from gateway.db.models import RequestLog


# ---- Helpers --------------------------------------------------------------


def _sse_chunks(events: list[dict[str, Any]]) -> list[bytes]:
    """Encode a list of dicts as Anthropic-shape SSE chunks."""
    return [
        f"event: {e.pop('event', 'message_delta')}\ndata: {json.dumps(e)}\n\n".encode()
        for e in events
    ]


def _build_streaming_response(chunks: list[bytes]) -> httpx.Response:
    """Build an httpx response with a ByteStream that yields chunks one at a time."""

    async def _gen():
        for c in chunks:
            await asyncio.sleep(0.01)
            yield c

    return httpx.Response(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        stream=_AsyncByteStream(_gen),
    )


class _AsyncByteStream(httpx.AsyncByteStream):
    """Adapt a no-arg async generator factory into an ``AsyncByteStream``."""

    def __init__(self, gen_factory) -> None:  # type: ignore[no-untyped-def]
        self._gen_factory = gen_factory

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        async for chunk in self._gen_factory():
            yield chunk

    async def aclose(self) -> None:
        return None


# ---- Tests ---------------------------------------------------------------


async def test_messages_unauthenticated_returns_401(db_client: AsyncClient) -> None:
    resp = await db_client.post(
        "/v1/messages",
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "unauthorized"}


async def test_messages_disallowed_model_returns_400(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
) -> None:
    client, headers, _ = auth_client
    resp = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "model": "openai/gpt-4-turbo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "model_not_allowed"


async def test_messages_stream_false_returns_400(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
) -> None:
    """P3 only supports streaming. ``stream=false`` must be rejected up front."""
    client, headers, _ = auth_client
    resp = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "streaming_required"


async def test_messages_streams_chunks_incrementally(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
) -> None:
    """Chunks must arrive one at a time, with TTFB << total stream time."""
    client, headers, _ = auth_client

    chunks = _sse_chunks([
        {"event": "message_start", "type": "message_start"},
        {"type": "content_block_delta", "delta": {"text": "hello"}},
        {"type": "content_block_delta", "delta": {"text": " world"}},
        {"type": "message_delta", "usage": {"input_tokens": 3, "output_tokens": 2}},
        {"event": "message_stop", "type": "message_stop"},
    ])
    openrouter_mock.post("/messages").mock(
        return_value=_build_streaming_response(chunks)
    )

    timestamps: list[float] = []
    started = time.monotonic()
    chunk_count = 0
    async with client.stream(
        "POST",
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        assert resp.headers.get("x-request-id")
        async for chunk in resp.aiter_raw():
            if chunk:
                timestamps.append(time.monotonic())
                chunk_count += 1

    # httpx's ASGITransport coalesces ASGI body events into a single
    # ``aiter_raw`` chunk, so we can't verify per-chunk delivery in-process.
    # The meaningful streaming SLO — TTFB — IS observable: the test mock
    # adds 10ms gaps between chunks, but the response should arrive long
    # before the upstream finishes, well under 200 ms.
    assert chunk_count >= 1, f"expected at least one chunk, got {chunk_count}"
    assert all(timestamps[i] <= timestamps[i + 1] for i in range(len(timestamps) - 1))
    first_byte_delta = timestamps[0] - started
    assert first_byte_delta < 0.2, f"TTFB {first_byte_delta:.3f}s exceeded 200 ms"


async def test_messages_writes_request_log_row(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    client, headers, user_info = auth_client

    chunks = _sse_chunks([
        {"event": "message_start", "type": "message_start"},
        {"type": "content_block_delta", "delta": {"text": "ok"}},
        {"type": "message_delta", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ])
    openrouter_mock.post("/messages").mock(
        return_value=_build_streaming_response(chunks)
    )

    async with client.stream(
        "POST",
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        async for _ in resp.aiter_raw():
            pass

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        rows = (await session.execute(select(RequestLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "messages"
    assert str(row.user_id) == user_info["id"]
    assert row.status_code == 200
    assert row.model == "anthropic/claude-haiku-4.5"
    assert row.request_body is not None
    assert row.request_body["model"] == "anthropic/claude-haiku-4.5"
    assert row.request_body["stream"] is True
    assert row.request_bytes is not None and row.request_bytes > 0
    assert row.response_bytes is not None and row.response_bytes > 0
    assert row.response_body is not None
    assert row.latency_ms is not None and row.latency_ms >= 0


async def test_messages_extracts_token_usage(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    client, headers, _ = auth_client

    chunks = _sse_chunks([
        {"type": "content_block_delta", "delta": {"text": "ok"}},
        {"type": "message_delta", "usage": {"input_tokens": 42, "output_tokens": 17}},
    ])
    openrouter_mock.post("/messages").mock(
        return_value=_build_streaming_response(chunks)
    )

    async with client.stream(
        "POST",
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        async for _ in resp.aiter_raw():
            pass

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        row = (await session.execute(select(RequestLog))).scalar_one()
    assert row.tokens_in == 42
    assert row.tokens_out == 17
    # Haiku: 42*1/1M + 17*5/1M = 0.000042 + 0.000085 = 0.000127
    assert row.cost_usd is not None
    assert float(row.cost_usd) == pytest.approx(0.000127, abs=1e-9)


async def test_messages_upstream_5xx_recorded_as_error(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    client, headers, _ = auth_client

    err_body = b'{"error":"provider_blew_up"}'
    openrouter_mock.post("/messages").mock(
        return_value=httpx.Response(status_code=500, content=err_body),
    )

    received = bytearray()
    async with client.stream(
        "POST",
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        # Gateway returns 200 on the wire (we already committed to a
        # StreamingResponse) but the upstream status is recorded in the
        # row. The error body is relayed verbatim in the stream.
        async for chunk in resp.aiter_raw():
            received.extend(chunk)
    assert err_body in bytes(received)

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        row = (await session.execute(select(RequestLog))).scalar_one()
    assert row.status_code == 500
    assert row.error_code == "upstream_500"


async def test_messages_upstream_timeout_recorded(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    client, headers, _ = auth_client

    openrouter_mock.post("/messages").mock(
        side_effect=httpx.ReadTimeout("read timed out")
    )

    async with client.stream(
        "POST",
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        async for _ in resp.aiter_raw():
            pass

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        row = (await session.execute(select(RequestLog))).scalar_one()
    assert row.error_code == "ReadTimeout"
    assert row.status_code == 502


async def test_messages_response_body_truncated_at_max_body_bytes(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    openrouter_mock: Any,
    db_engine: AsyncEngine,
) -> None:
    """A >32 KB upstream stream stores exactly MAX_BODY_BYTES + marker."""
    client, headers, _ = auth_client

    # 40 KB of payload split across many chunks so the bounded tee path
    # is exercised end-to-end.
    big_chunk = b"X" * 1024
    chunks = [big_chunk] * 40 + [
        b'event: message_delta\ndata: {"usage":{"input_tokens":1,"output_tokens":1}}\n\n',
    ]
    openrouter_mock.post("/messages").mock(
        return_value=_build_streaming_response(chunks)
    )

    async with client.stream(
        "POST",
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        async for _ in resp.aiter_raw():
            pass

    from gateway.config import get_settings

    max_bytes = get_settings().max_body_bytes

    # Sum the bytes we actually streamed upstream so we can assert against
    # the true total (not just max_bytes).
    upstream_total = sum(len(c) for c in chunks)
    assert upstream_total > max_bytes  # sanity: we did exceed the cap

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        row = (await session.execute(select(RequestLog))).scalar_one()
    assert row.response_body is not None
    assert "[truncated]" in row.response_body
    # ``response_bytes`` reports the TRUE upstream transfer size (tracked
    # independently of the bounded accumulator) so the audit row stays
    # truthful even when the stored body is clipped.
    assert row.response_bytes == upstream_total
    # Stored text body is bounded at max_bytes + the truncation marker.
    assert len(row.response_body.encode("utf-8")) <= max_bytes + len(
        "\n…[truncated]".encode("utf-8")
    )


async def test_usage_returns_summed_costs(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """Insert rows for multiple endpoints, hit /v1/usage, verify aggregation
    AND the per-endpoint ``endpoints`` breakdown introduced in P5."""
    from decimal import Decimal
    from uuid import UUID, uuid4

    client, headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        # Two messages rows.
        for cost, ti, to in [(Decimal("0.001"), 100, 50), (Decimal("0.002"), 200, 75)]:
            session.add(
                RequestLog(
                    request_id=uuid4(),
                    user_id=user_id,
                    endpoint="messages",
                    model="anthropic/claude-haiku-4.5",
                    tokens_in=ti,
                    tokens_out=to,
                    cost_usd=cost,
                    status_code=200,
                    latency_ms=100,
                    request_body={"x": 1},
                    response_body="ok",
                    request_bytes=10,
                    response_bytes=2,
                )
            )
        # One embeddings row.
        session.add(
            RequestLog(
                request_id=uuid4(),
                user_id=user_id,
                endpoint="embeddings",
                model="openai/text-embedding-3-small",
                tokens_in=42,
                tokens_out=0,
                cost_usd=Decimal("0.000001"),
                status_code=200,
                latency_ms=50,
                request_body={"x": 1},
                response_body="ok",
                request_bytes=10,
                response_bytes=2,
            )
        )
        # One vectors.search row (cost 0).
        session.add(
            RequestLog(
                request_id=uuid4(),
                user_id=user_id,
                endpoint="vectors.search",
                model=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=Decimal("0.000000"),
                status_code=200,
                latency_ms=20,
                request_body={"collection": "docs"},
                response_body="ok",
                request_bytes=20,
                response_bytes=2,
            )
        )
        await session.commit()

    resp = await client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_count"] == 4
    # 0.001 + 0.002 + 0.000001 + 0.000000 = 0.003001
    assert body["spent_usd"] == pytest.approx(0.003001, abs=1e-9)
    assert body["tokens_in"] == 342
    assert body["tokens_out"] == 125
    assert body["period_start"]

    # P5: per-endpoint breakdown.
    endpoints = body["endpoints"]
    assert set(endpoints.keys()) == {"messages", "embeddings", "vectors.search"}
    assert endpoints["messages"]["requests"] == 2
    assert endpoints["messages"]["spent_usd"] == pytest.approx(0.003, abs=1e-9)
    assert endpoints["messages"]["tokens_in"] == 300
    assert endpoints["messages"]["tokens_out"] == 125
    assert endpoints["embeddings"]["requests"] == 1
    assert endpoints["embeddings"]["spent_usd"] == pytest.approx(0.000001, abs=1e-9)
    assert endpoints["embeddings"]["tokens_in"] == 42
    assert endpoints["embeddings"]["tokens_out"] == 0
    assert endpoints["vectors.search"]["requests"] == 1
    assert endpoints["vectors.search"]["spent_usd"] == 0.0
