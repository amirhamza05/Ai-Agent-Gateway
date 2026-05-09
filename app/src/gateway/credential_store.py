"""DB-backed credential store with env-var fallback.

Manages the three server-side secrets that can be configured from the
admin dashboard without a redeploy:

  * ``openrouter_api_key``
  * ``qdrant_url``
  * ``qdrant_api_key``

Resolution order: gateway_settings table → env var from Settings.
If neither source has a value, ``resolve()`` raises :exc:`CredentialMissing`
and the route returns 503 to the caller.

Uses the same 30-second TTL async-cache pattern as ``PricingCache``
in :mod:`gateway.billing`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from gateway.db.models import GatewaySettings

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

    from gateway.config import Settings

logger = structlog.get_logger(__name__)

SETTING_OPENROUTER_KEY = "openrouter_api_key"
SETTING_QDRANT_URL = "qdrant_url"
SETTING_QDRANT_KEY = "qdrant_api_key"

_ALL_KEYS = {SETTING_OPENROUTER_KEY, SETTING_QDRANT_URL, SETTING_QDRANT_KEY}


class CredentialMissing(Exception):
    """Raised when a required credential has no DB row and no env-var fallback."""


class CredentialStore:
    """30-second in-process TTL cache for ``gateway_settings`` rows.

    Reads from the DB and caches the snapshot for ``ttl_seconds``. Falls
    back to the process-level :class:`~gateway.config.Settings` (env vars)
    when a DB row is absent. Invalidated by dashboard mutations so changes
    are visible within a few hundred milliseconds on the worker that
    handled the form POST.
    """

    def __init__(self, settings: Settings, ttl_seconds: float = 30.0) -> None:
        self._settings = settings
        self._ttl = ttl_seconds
        self._snapshot: dict[str, str] | None = None
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _load(self, session: AsyncSession) -> dict[str, str]:
        now = time.monotonic()
        if self._snapshot is not None and (now - self._loaded_at) < self._ttl:
            return self._snapshot

        async with self._lock:
            now = time.monotonic()
            if self._snapshot is not None and (now - self._loaded_at) < self._ttl:
                return self._snapshot

            result = await session.execute(select(GatewaySettings))
            rows = result.scalars().all()
            snapshot = {row.key: row.value for row in rows if row.value}
            self._snapshot = snapshot
            self._loaded_at = time.monotonic()
            return snapshot

    async def resolve(self, key: str, session: AsyncSession) -> str:
        """Return the credential value for ``key``.

        Checks the DB snapshot first; falls back to the env var. Raises
        :exc:`CredentialMissing` when neither source has a value so the
        caller can return 503 to the client rather than crashing.
        """
        snapshot = await self._load(session)
        if key in snapshot:
            return snapshot[key]

        s = self._settings
        if key == SETTING_OPENROUTER_KEY and s.openrouter_api_key is not None:
            return s.openrouter_api_key.get_secret_value()
        if key == SETTING_QDRANT_URL and s.qdrant_url is not None:
            return s.qdrant_url
        if key == SETTING_QDRANT_KEY and s.qdrant_api_key is not None:
            return s.qdrant_api_key.get_secret_value()

        raise CredentialMissing(key)

    async def get_db_values(self, session: AsyncSession) -> dict[str, str]:
        """Return only DB-stored values (no env fallback).

        Used by the dashboard settings page to show what has been
        explicitly configured, without revealing env-var secrets.
        """
        return dict(await self._load(session))

    def invalidate(self) -> None:
        """Drop the cached snapshot so the next ``resolve`` re-reads the DB.

        Called by the dashboard settings POST handler after committing
        new credential values.
        """
        self._snapshot = None
        self._loaded_at = 0.0
