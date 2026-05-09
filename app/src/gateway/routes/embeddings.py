"""``POST /v1/embeddings`` — non-streaming OpenAI-shape passthrough.

Unlike ``/v1/messages`` (streaming, all the SSE machinery), embeddings
are JSON-in / JSON-out. A single ``await client.post(...)`` is enough,
the response fits in memory, and we can compute cost at the end of the
call rather than parsing usage out of an SSE stream.

Logging discipline matches the messages route: one ``request_log`` row
per call written from a ``try/finally`` so failed upstream calls (4xx,
5xx, network error) still produce an audit row. ``cost_usd`` is computed
from ``usage.prompt_tokens`` via :func:`compute_embedding_cost_usd`.

The handler is gated by :func:`enforce_monthly_cap` (which chains
through ``enforce_rate_limit`` → ``require_user``) so a single
``Depends`` does auth → rate-limit → cap before we open any outbound
connection. Same pattern as the messages route — see
:mod:`gateway.limits` for the chain.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session
from gateway.billing import compute_embedding_cost_usd
from gateway.config import Settings, get_settings
from gateway.db.models import User
from gateway.limits import enforce_monthly_cap
from gateway.logging_mw import insert_request_log
from gateway.truncate import truncate
from gateway.upstream.openrouter import call_embeddings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["embeddings"])


class EmbeddingsRequest(BaseModel):
    """Pass-through embeddings body, validated at the gateway boundary.

    ``extra="forbid"`` is intentional. Embeddings have a small, stable
    surface (model + input + format), and accepting unknown fields would
    let a misconfigured client (or a typo) reach OpenRouter unchecked
    and bill against the user's cap. If a future upstream adds new
    fields we add them here explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    # min_length=1 rejects empty arrays (would still cost a request to
    # OpenRouter with no useful result). max_length=512 caps the per-call
    # bill so a single user can't push thousands of strings through one
    # request and bypass the rate limiter's per-call accounting.
    input: list[str] = Field(min_length=1, max_length=512)
    encoding_format: Literal["float", "base64"] | None = None


@router.post("/embeddings", summary="OpenAI-shape embeddings passthrough")
async def embeddings(
    body: EmbeddingsRequest,
    request: Request,
    user: User = Depends(enforce_monthly_cap),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Forward to OpenRouter's ``/embeddings`` and log the result.

    Validation:

    * ``body.model`` must be in ``ALLOWED_MODELS`` — 400 otherwise. The
      same allow-list gates ``/v1/messages``; this is documented in
      ``.env.example`` so operators don't get surprised.

    Side effects:

    * Writes one ``request_log`` row in the ``finally`` block, regardless
      of success/error.
    * Echoes the gateway-issued ``request_id`` UUID in ``X-Request-Id``.
    * Cost is computed from ``usage.prompt_tokens`` on the upstream
      response. ``tokens_out`` is always 0 — embeddings have no text
      output.
    """
    # Validate against the DB-backed pricing table. The cache snapshot
    # serves both the allow-list and pricing lookups with one SELECT.
    pricing_cache = request.app.state.pricing_cache
    prices = await pricing_cache.get_all(session)
    price_row = prices.get(body.model)
    if (
        price_row is None
        or price_row.endpoint_kind != "embeddings"
        or not price_row.is_allowed
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "model_not_allowed", "model": body.model},
        )

    request_id = uuid4()
    chat_id = request.headers.get("X-Chat-Id") or None
    started = time.monotonic()
    log = logger.bind(
        request_id=str(request_id),
        user_id=str(user.id),
        endpoint="embeddings",
        model=body.model,
    )

    upstream_body: dict[str, Any] = {"model": body.model, "input": body.input}
    if body.encoding_format is not None:
        upstream_body["encoding_format"] = body.encoding_format

    client: httpx.AsyncClient = request.app.state.openrouter_client

    state: dict[str, Any] = {
        "status_code": 0,
        "error_code": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": None,
        "response_text": "",
        "response_bytes": 0,
        "response_payload": None,
    }

    try:
        try:
            resp, parsed = await call_embeddings(
                client,
                settings=settings,
                model=body.model,
                inputs=body.input,
            )
        except httpx.HTTPError as exc:
            # Network-level failure: timeout, connect error, etc. Surface
            # as 502 to the client but still log the row.
            state["error_code"] = type(exc).__name__
            state["status_code"] = status.HTTP_502_BAD_GATEWAY
            log.warning("embeddings.upstream_error", error_type=type(exc).__name__)
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"error": state["error_code"]},
                headers={"X-Request-Id": str(request_id)},
            )

        state["status_code"] = resp.status_code
        # ``resp.content`` was already buffered by ``client.post`` (no
        # streaming) so this is a cheap byte-count, no extra read.
        state["response_bytes"] = len(resp.content)
        state["response_text"] = (
            resp.text if isinstance(resp.text, str) else resp.content.decode("utf-8", "replace")
        )
        state["response_payload"] = parsed

        if resp.status_code >= 400:
            state["error_code"] = f"upstream_{resp.status_code}"
            # Pass-through the upstream body verbatim. If we couldn't
            # parse JSON, send back a structured error wrapper so the
            # add-in's parser doesn't choke on raw HTML.
            content: Any
            if parsed is not None:
                content = parsed
            else:
                content = {"error": state["error_code"]}
            return JSONResponse(
                status_code=resp.status_code,
                content=content,
                headers={"X-Request-Id": str(request_id)},
            )

        # Happy path: extract token usage for cost accounting and pass
        # the upstream JSON through unchanged.
        if parsed is not None:
            usage = parsed.get("usage") or {}
            tokens_in_raw = usage.get("prompt_tokens", 0)
            try:
                state["tokens_in"] = int(tokens_in_raw or 0)
            except (TypeError, ValueError):
                state["tokens_in"] = 0
            state["cost_usd"] = compute_embedding_cost_usd(
                body.model,
                state["tokens_in"],
                prices=prices,
            )
        else:
            # 2xx with non-JSON body. Vanishingly rare for OpenRouter,
            # but we don't want to raise — log and return what we got.
            log.info("embeddings.non_json_2xx")

        return JSONResponse(
            status_code=resp.status_code,
            content=parsed if parsed is not None else {},
            headers={"X-Request-Id": str(request_id)},
        )
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        max_bytes = settings.max_body_bytes

        request_body_text, request_bytes = truncate(
            json.dumps(upstream_body), max_bytes
        )
        response_text = state["response_text"] or ""
        response_body_text, _ = truncate(response_text, max_bytes)
        response_bytes = state["response_bytes"] or len(
            response_text.encode("utf-8")
        )

        cost: Decimal | None = state["cost_usd"]

        try:
            await insert_request_log(
                session,
                request_id=request_id,
                user_id=user.id,
                endpoint="embeddings",
                model=body.model,
                tokens_in=state["tokens_in"],
                tokens_out=state["tokens_out"],
                cost_usd=cost,
                status_code=state["status_code"] or None,
                error_code=state["error_code"],
                latency_ms=latency_ms,
                client_version=request.headers.get("X-Client-Version"),
                client_ip=request.client.host if request.client else None,
                request_body=upstream_body,
                response_body=response_body_text,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                meta={"input_count": len(body.input)},
                chat_id=chat_id,
            )
            await session.commit()
            log.info(
                "embeddings.completed",
                status_code=state["status_code"],
                latency_ms=latency_ms,
                tokens_in=state["tokens_in"],
                cost_usd=str(cost) if cost is not None else None,
            )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:  # pragma: no cover
                pass
            log.exception("embeddings.request_log_insert_failed")
