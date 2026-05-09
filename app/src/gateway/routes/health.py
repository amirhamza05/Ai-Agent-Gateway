"""Liveness / readiness endpoint.

``GET /healthz`` returns ``{"ok": True, "version": "<app version>"}`` and
requires no auth. The Docker HEALTHCHECK and Caddy upstream-checking both
hit it; do not add latency-sensitive work here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from gateway.config import Settings, get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    """Return a small JSON document confirming the process is alive."""
    return {"ok": True, "version": settings.version}
