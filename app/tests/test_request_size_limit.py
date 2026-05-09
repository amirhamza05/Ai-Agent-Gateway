"""Tests for ``gateway.middleware.RequestSizeLimitMiddleware``.

Two flavours: directly against the ASGI app via ``AsyncClient``
(unit-ish, no DB), and against the auth/messages endpoints with
``content-length`` set to confirm the rejection happens BEFORE auth
runs (the middleware is registered first in the stack).
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.middleware import RequestSizeLimitMiddleware


def _build_test_app(max_bytes: int) -> FastAPI:
    """Tiny FastAPI app with the middleware and one echo route.

    Self-contained so the size-limit tests don't drag in the full
    gateway lifespan (Postgres, Redis, etc.).
    """
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=max_bytes)

    @app.post("/echo")
    async def echo(payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {"received_keys": list(payload.keys()), "size": len(json.dumps(payload))}

    return app


async def test_normal_size_passes() -> None:
    """A small body well under the cap reaches the handler unmodified."""
    app = _build_test_app(max_bytes=1024)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/echo", json={"hello": "world"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["received_keys"] == ["hello"]


async def test_oversized_content_length_returns_413() -> None:
    """A request whose Content-Length exceeds the cap is rejected at the door.

    Using a manual ``content`` payload rather than ``json=`` so we
    control the byte count exactly.
    """
    cap = 100
    app = _build_test_app(max_bytes=cap)

    big_payload = ("x" * (cap + 50)).encode("utf-8")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/echo",
            content=big_payload,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 413
    body = resp.json()
    assert body["detail"]["error"] == "request_too_large"


async def test_just_under_cap_passes() -> None:
    """Exactly ``max_bytes`` is allowed; ``max_bytes + 1`` is rejected.

    Boundary check — off-by-one bugs in size checks are common.
    """
    cap = 64
    app = _build_test_app(max_bytes=cap)

    # The handler expects JSON, so build one that lands at exactly cap
    # bytes when serialised.
    base = '{"k":"'
    suffix = '"}'
    pad_len = cap - len(base) - len(suffix)
    assert pad_len > 0
    payload = (base + ("x" * pad_len) + suffix).encode("utf-8")
    assert len(payload) == cap

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/echo",
            content=payload,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 200

    # One byte over: 413.
    over = payload[:-1] + b'x' + payload[-1:]
    assert len(over) == cap + 1
    transport2 = ASGITransport(app=app)
    async with AsyncClient(transport=transport2, base_url="http://test") as ac:
        resp = await ac.post(
            "/echo",
            content=over,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 413


async def test_get_requests_unaffected() -> None:
    """GET has no body — middleware must not interfere."""
    app = _build_test_app(max_bytes=10)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_413_runs_before_auth_via_full_app(client: AsyncClient) -> None:
    """End-to-end: the size cap fires BEFORE auth on the real app.

    The full gateway app declares the middleware ahead of all routers,
    so an unauthenticated POST with an over-sized body should 413 (not
    401). This is the security property that matters: a malicious
    client must not be able to force the worker to buffer megabytes
    of content just to get told to log in.
    """
    from gateway.config import get_settings

    settings = get_settings()
    cap = settings.max_body_bytes * 4
    big_payload = b"x" * (cap + 1024)

    resp = await client.post(
        "/v1/messages",
        content=big_payload,
        headers={"Content-Type": "application/json"},
    )
    # 413 wins over 401 because middleware runs first.
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "request_too_large"
