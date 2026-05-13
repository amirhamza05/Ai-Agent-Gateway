"""Embedding-provider preflight check for the /dashboard/vectordb pages.

Fires a tiny ``["ping"]`` embedding request via OpenRouter and caches the
result for 60 seconds so the status pill and form guards don't spam the
upstream API on every page load.

The cache is invalidated when any upstream response returns an error
(so a recently-fixed credential takes effect on the next user action)
and on an explicit :func:`invalidate_embed_status` call.

Error categories (surfaced to the template, never the raw key/body):
    ``credential_missing``  — CredentialStore raised CredentialMissing.
    ``model_not_allowed``   — ``openai/text-embedding-3-small`` absent from
                             the DB pricing table or not allowed.
    ``upstream_error``      — HTTP 4xx/5xx or network failure from OpenRouter.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession

    from gateway.billing import PricingCache
    from gateway.credential_store import CredentialStore

logger = structlog.get_logger(__name__)

# The model used for the preflight ping and for all dashboard embed operations.
EMBED_MODEL = "openai/text-embedding-3-small"

# TTL for a successful or failed preflight result.
_TTL_SECONDS = 60.0


class EmbedProviderStatus:
    """60-second TTL in-process cache for the embedding-provider health check.

    One instance is created in the FastAPI lifespan and stored on
    ``app.state.embed_provider_status``.  The cache is per-worker; a
    multi-worker deploy may see up to 60 s of stale status on workers that
    haven't yet hit their TTL.
    """

    def __init__(self) -> None:
        self._ok: bool | None = None
        self._reason: str | None = None
        self._checked_at: float = 0.0
        self._lock = asyncio.Lock()

    def _is_fresh(self) -> bool:
        return (
            self._ok is not None
            and (time.monotonic() - self._checked_at) < _TTL_SECONDS
        )

    def invalidate(self) -> None:
        """Drop the cached result; the next call will re-probe the provider."""
        self._ok = None
        self._checked_at = 0.0

    async def check(
        self,
        *,
        client: "httpx.AsyncClient",
        cred_store: "CredentialStore",
        pricing_cache: "PricingCache",
        session: "AsyncSession",
        base_url: str,
    ) -> tuple[bool, str | None]:
        """Return ``(ok, reason)`` for the embedding provider.

        Caches the result for 60 seconds.  On error the cache is *not*
        stored so the next request re-probes immediately (gives fast
        recovery without hammering the upstream on every request).
        """
        if self._is_fresh():
            return self._ok, self._reason  # type: ignore[return-value]

        async with self._lock:
            # Double-checked locking: another coroutine may have refreshed
            # while we were waiting for the lock.
            if self._is_fresh():
                return self._ok, self._reason  # type: ignore[return-value]

            ok, reason = await _probe(
                client=client,
                cred_store=cred_store,
                pricing_cache=pricing_cache,
                session=session,
                base_url=base_url,
            )
            # Only cache the result on success.  On failure, keep _ok=None
            # so the next caller re-probes (fast recovery path).
            if ok:
                self._ok = True
                self._reason = None
                self._checked_at = time.monotonic()
            else:
                # Store the failure so the *current* request sees the
                # reason, but do NOT advance _checked_at so the TTL does
                # not protect stale failure results.
                self._ok = False
                self._reason = reason
                # Intentionally leave _checked_at at 0 so _is_fresh() stays
                # False and the next request re-probes.
                logger.warning(
                    "dashboard.embed_provider_unavailable",
                    reason=reason,
                )
            return ok, reason


async def _probe(
    *,
    client: "httpx.AsyncClient",
    cred_store: "CredentialStore",
    pricing_cache: "PricingCache",
    session: "AsyncSession",
    base_url: str,
) -> tuple[bool, str | None]:
    """Fire a single embed request and return ``(ok, reason)``."""
    from gateway.credential_store import CredentialMissing, SETTING_OPENROUTER_KEY
    from gateway.upstream.openrouter import call_embeddings
    import httpx as _httpx

    # 1. Credential check
    try:
        api_key = await cred_store.resolve(SETTING_OPENROUTER_KEY, session)
    except CredentialMissing:
        return False, "credential_missing"

    # 2. Model allow-list check
    try:
        prices = await pricing_cache.get_all(session)
        row = prices.get(EMBED_MODEL)
        if row is None or row.endpoint_kind != "embeddings" or not row.is_allowed:
            return False, "model_not_allowed"
    except Exception:
        # Pricing cache failure should not hard-block; treat as unknown.
        return False, "upstream_error"

    # 3. Actual upstream probe
    try:
        resp, parsed = await call_embeddings(
            client,
            api_key=api_key,
            base_url=base_url,
            model=EMBED_MODEL,
            inputs=["ping"],
        )
    except _httpx.HTTPError as exc:
        # Network-level error — category only, no key or body leaked.
        return False, f"upstream_error: {type(exc).__name__}"

    if resp.status_code >= 400:
        # Surface category + a safe tail of the status, not the full body.
        tail = f"HTTP {resp.status_code}"
        return False, f"upstream_error: {tail}"

    return True, None
