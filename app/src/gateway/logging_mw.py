"""Request-log persistence helper.

Despite the ``_mw`` suffix (reserved for the future ASGI middleware that
will log non-streaming endpoints), Phase 3 doesn't add any middleware.
Streaming responses can't be cleanly observed from a middleware — the
``finally`` of the streaming generator is the only place where TTFB,
upstream status, accumulated body, and parsed token usage all coexist.
So the streaming handler in :mod:`gateway.routes.messages` calls
:func:`insert_request_log` directly.

P4 / P5 will add a middleware in this same module for the non-streaming
``/v1/embeddings`` and ``/v1/vectors/*`` endpoints. Keeping the helper
function here means the streaming and non-streaming paths converge on
one INSERT shape.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db.models import RequestLog

logger = structlog.get_logger(__name__)


async def insert_request_log(
    session: AsyncSession,
    *,
    request_id: UUID,
    user_id: UUID | None,
    endpoint: str,
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: Decimal | None,
    status_code: int | None,
    error_code: str | None,
    latency_ms: int | None,
    client_version: str | None,
    client_ip: str | None,
    request_body: dict | None,
    response_body: str | None,
    request_bytes: int | None,
    response_bytes: int | None,
    meta: dict | None,
    chat_id: str | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> None:
    """Append a row to ``request_log``.

    Caller owns the commit. The streaming handler commits inside its
    ``finally`` block so the row is durable even when the client
    disconnects mid-stream — relying on FastAPI's dependency teardown to
    commit would silently drop rows on cancellation.

    All fields are passed as keyword args because the signature is wide
    enough that positional ordering would be a maintenance hazard.

    The session is NOT flushed here; SQLAlchemy will flush on the
    caller's commit. We also don't ``session.refresh(row)`` because the
    server-side ``id`` and ``created_at`` aren't read by the streaming
    handler — they're only ever read by ``/v1/usage`` and operator psql
    sessions.
    """
    row = RequestLog(
        request_id=request_id,
        user_id=user_id,
        endpoint=endpoint,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        status_code=status_code,
        error_code=error_code,
        latency_ms=latency_ms,
        client_version=client_version,
        client_ip=client_ip,
        request_body=request_body,
        response_body=response_body,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        meta=meta,
        chat_id=chat_id,
    )
    session.add(row)
