"""Tests for the /healthz endpoint."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_returns_ok(client: AsyncClient) -> None:
    """GET /healthz should return 200 with ``ok=True`` and a version string."""
    response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body.get("version"), str)
    assert body["version"]  # non-empty


async def test_healthz_requires_no_auth(client: AsyncClient) -> None:
    """The liveness probe must work without any Authorization header."""
    response = await client.get("/healthz", headers={})
    assert response.status_code == 200
