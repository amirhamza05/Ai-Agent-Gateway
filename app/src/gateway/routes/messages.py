"""``POST /v1/messages`` — Anthropic-shape streaming passthrough.

This is the most failure-prone endpoint in the gateway. The two cardinal
rules from §8.2 of the plan:

1. **Yield each upstream chunk to the client immediately.** Never call
   ``await resp.aread()`` (kills first-token latency) and never coalesce
   chunks before yielding (defeats the whole point of SSE).
2. **Bound the tee accumulator at ``MAX_BODY_BYTES``.** Slice the chunk
   before appending so a chatty stream can't OOM the worker.

Wire format is byte-for-byte the Anthropic Messages API shape that
OpenRouter exposes. The add-in's ``AnthropicClient`` only changes
``ApiUrlFormat`` — no SDK changes.

Logging happens in the streaming generator's ``finally`` block. That's
the only place where TTFB, upstream status, accumulated body, and parsed
token usage all coexist; an ASGI middleware can't see them. The session
commits explicitly inside the ``finally`` because the FastAPI dependency
teardown can't be relied on to commit on client cancellation.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session
from gateway.billing import compute_cost_usd, is_model_allowed
from gateway.config import Settings, get_settings
from gateway.credential_store import CredentialMissing, CredentialStore, SETTING_OPENROUTER_KEY
from gateway.db.models import User
from gateway.limits import enforce_monthly_cap, enforce_token_model_scope
from gateway.logging_mw import insert_request_log
from gateway.routes._sse_parse import extract_usage_full
from gateway.truncate import truncate, truncate_bytes
from gateway.upstream.openrouter import auth_headers

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["messages"])

# Keep the trailing tail we scan for ``"usage": {...}`` small. Anthropic
# emits the final usage object near end-of-stream, so a few KB suffices.
# Larger windows just increase work-per-chunk for no benefit.
_USAGE_SCAN_TAIL_BYTES = 4096


class MessagesRequest(BaseModel):
    """Pass-through Anthropic Messages API body.

    ``extra="allow"`` so unknown fields (e.g. ``thinking``, ``tool_choice``,
    new fields Anthropic adds) flow upstream unmodified. P3 only validates
    ``model`` (against ``ALLOWED_MODELS``), the presence of ``messages``,
    and that ``stream`` isn't false (P3 only supports streaming).
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    stream: bool = True


@router.post("/messages", summary="Streaming Anthropic Messages passthrough")
async def messages(
    body: MessagesRequest,
    request: Request,
    # ``enforce_monthly_cap`` chains through ``enforce_rate_limit`` and
    # ``require_user`` (see :mod:`gateway.limits`), so a single dependency
    # on this handler runs auth → rate-limit → cap in the right order.
    # We swap ``require_user`` for it here in P4; the parameter type is
    # unchanged.
    user: User = Depends(enforce_monthly_cap),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Proxy to OpenRouter's ``/messages``, streaming chunks back verbatim.

    Validation:

    * ``model`` must be in ``ALLOWED_MODELS`` — 400 otherwise.
    * ``stream`` must be true for P3 — 400 otherwise. (Non-streaming
      responses are P5+ if needed at all; the add-in always streams.)

    Side effects:

    * Writes one ``request_log`` row in the generator's ``finally``,
      regardless of success/error/timeout.
    * Echoes the gateway-issued ``request_id`` UUID in ``X-Request-Id``.
    """
    # Pre-stream validation — must raise BEFORE we open any upstream
    # connection or build a StreamingResponse, so the client gets a
    # normal JSON 4xx instead of an SSE-shaped response.
    #
    # The model allow-list and pricing both live in the ``model_pricing``
    # DB table now; the cache snapshot serves both lookups with one
    # SELECT. ``is_model_allowed`` checks endpoint_kind="messages" via
    # the snapshot and respects ``is_allowed`` + soft-delete.
    pricing_cache = request.app.state.pricing_cache
    prices = await pricing_cache.get_all(session)
    price_row = prices.get(body.model)
    if (
        price_row is None
        or price_row.endpoint_kind != "messages"
        or not price_row.is_allowed
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "model_not_allowed", "model": body.model},
        )

    # Per-token scope: reject when the bearer JWT was minted from an
    # api_token whose ``allow_all_models`` is False and the requested
    # model isn't in its allow-list. No-op for user-level JWTs.
    await enforce_token_model_scope(request, session, model=body.model)

    if body.stream is False:
        return await _messages_unary(body, request, user, session, settings, prices)

    request_id = uuid4()
    chat_id = request.headers.get("X-Chat-Id") or None
    started = time.monotonic()
    log = logger.bind(
        request_id=str(request_id),
        user_id=str(user.id),
        endpoint="messages",
        model=body.model,
    )

    # Force streaming on the upstream. ``exclude_none=True`` keeps the
    # body lean — null fields are ignored by Anthropic anyway and bloat
    # request_log JSONB rows.
    upstream_body: dict[str, Any] = body.model_dump(exclude_none=True) | {"stream": True}

    client: httpx.AsyncClient = request.app.state.openrouter_client
    url = f"{settings.openrouter_base_url}/messages"

    cred_store: CredentialStore = request.app.state.credential_store
    try:
        or_api_key = await cred_store.resolve(SETTING_OPENROUTER_KEY, session)
    except CredentialMissing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "service_not_configured", "key": "openrouter_api_key"},
        )

    # Mutable per-request state shared with the generator's ``finally``.
    # ``nonlocal`` keeps everything in one async closure — no instance
    # class needed.
    state: dict[str, Any] = {
        "first_byte_at": None,
        "upstream_status": 0,
        "error_code": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "usage_seen": False,
        # ``response_bytes_total`` tracks the TRUE upstream byte count
        # (incremented per chunk before any clipping). This is what we
        # store in ``request_log.response_bytes`` — independent of the
        # bounded accumulator so the audit row reflects the real upstream
        # transfer size even when the body was truncated.
        "response_bytes_total": 0,
    }
    accumulated = bytearray()
    max_bytes = settings.max_body_bytes

    async def gen() -> AsyncIterator[bytes]:
        try:
            async with client.stream(
                "POST",
                url,
                json=upstream_body,
                headers=auth_headers(or_api_key),
            ) as resp:
                state["upstream_status"] = resp.status_code

                # Error path: read a bounded chunk so we can log + relay
                # the upstream error verbatim. NEVER ``aread()`` — that
                # would also kill latency on the happy path if a typo
                # ever moved this branch out.
                if resp.status_code >= 400:
                    err = bytearray()
                    async for chunk in resp.aiter_raw():
                        if state["first_byte_at"] is None:
                            state["first_byte_at"] = time.monotonic()
                        state["response_bytes_total"] += len(chunk)
                        remaining = max_bytes - len(err)
                        if remaining > 0:
                            err.extend(chunk[:remaining])
                        # Drain a bit further so the client sees the
                        # complete error body, but bounded so we don't
                        # buffer huge HTML error pages.
                        if len(err) >= max_bytes:
                            break
                    state["error_code"] = f"upstream_{resp.status_code}"
                    accumulated.extend(err[:max_bytes])
                    yield bytes(err)
                    return

                # Happy path: tee chunks to the client and to a bounded
                # accumulator. Keep an asciifield-ish trailing buffer so
                # we can scan for ``"usage": {...}`` without re-decoding
                # the full accumulator on every chunk.
                tail = bytearray()
                async for chunk in resp.aiter_raw():
                    if state["first_byte_at"] is None:
                        state["first_byte_at"] = time.monotonic()

                    # Track the true upstream byte count separately from
                    # the bounded accumulator so request_log.response_bytes
                    # reflects the real transfer size even when the body
                    # was truncated.
                    state["response_bytes_total"] += len(chunk)

                    # Bounded tee: slice BEFORE appending so we never
                    # over-allocate by a full chunk's worth.
                    if len(accumulated) < max_bytes:
                        accumulated.extend(chunk[: max_bytes - len(accumulated)])

                    # Maintain the trailing scan window. Trim from the
                    # front so its size is bounded regardless of stream
                    # length.
                    tail.extend(chunk)
                    if len(tail) > _USAGE_SCAN_TAIL_BYTES:
                        del tail[: len(tail) - _USAGE_SCAN_TAIL_BYTES]

                    # Cheap pre-check before invoking the regex. Avoids
                    # a regex pass on every chunk that doesn't contain a
                    # usage event (i.e. almost all of them).
                    if b'"usage"' in tail:
                        in_t, out_t, cr_t, cw_t = extract_usage_full(
                            tail.decode("utf-8", errors="ignore")
                        )
                        if (
                            in_t is not None
                            or out_t is not None
                            or cr_t is not None
                            or cw_t is not None
                        ):
                            state["usage_seen"] = True
                            if in_t is not None:
                                state["tokens_in"] = in_t
                            if out_t is not None:
                                state["tokens_out"] = out_t
                            if cr_t is not None:
                                state["cache_read_tokens"] = cr_t
                            if cw_t is not None:
                                state["cache_write_tokens"] = cw_t

                    yield bytes(chunk)
        except httpx.HTTPError as exc:
            # Network-level failure (timeout, connect error, etc.).
            # Surface to the client as a synthetic SSE error event so the
            # add-in's parser doesn't see a hard disconnect.
            state["error_code"] = type(exc).__name__
            state["upstream_status"] = status.HTTP_502_BAD_GATEWAY
            log.warning("messages.upstream_error", error_type=type(exc).__name__)
            err_payload = json.dumps({"error": state["error_code"]})
            yield f"data: {err_payload}\n\n".encode()
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            ttfb_ms = (
                int((state["first_byte_at"] - started) * 1000)
                if state["first_byte_at"] is not None
                else None
            )

            # One-shot final scan in case the usage event landed in the
            # very last chunk and the per-chunk scan window had already
            # rotated past it. The accumulator is bounded so this is
            # cheap.
            if not state["usage_seen"] and accumulated:
                in_t, out_t, cr_t, cw_t = extract_usage_full(
                    bytes(accumulated).decode("utf-8", errors="ignore")
                )
                if in_t is not None:
                    state["tokens_in"] = in_t
                    state["usage_seen"] = True
                if out_t is not None:
                    state["tokens_out"] = out_t
                    state["usage_seen"] = True
                if cr_t is not None:
                    state["cache_read_tokens"] = cr_t
                    state["usage_seen"] = True
                if cw_t is not None:
                    state["cache_write_tokens"] = cw_t
                    state["usage_seen"] = True

            if not state["usage_seen"] and state["error_code"] is None:
                # Don't spam this on error paths — only when the upstream
                # call ostensibly succeeded but didn't carry a usage event.
                log.info("messages.usage_not_found")

            request_body_text, request_bytes = truncate(
                json.dumps(upstream_body), max_bytes
            )
            # The accumulator is pre-bounded at ``max_bytes`` so
            # ``truncate_bytes`` never sees an over-cap buffer and never
            # appends its own marker. Append it manually based on the
            # running total. ``response_bytes`` carries the true upstream
            # transfer size regardless of the stored body's length.
            response_bytes = state["response_bytes_total"] or len(accumulated)
            response_body_text = bytes(accumulated).decode("utf-8", errors="ignore")
            if response_bytes > max_bytes:
                response_body_text += "\n…[truncated]"
            cost = compute_cost_usd(
                body.model,
                state["tokens_in"],
                state["tokens_out"],
                cache_read_tokens=state["cache_read_tokens"],
                cache_write_tokens=state["cache_write_tokens"],
                prices=prices,
            )

            try:
                await insert_request_log(
                    session,
                    request_id=request_id,
                    user_id=user.id,
                    endpoint="messages",
                    model=body.model,
                    tokens_in=state["tokens_in"],
                    tokens_out=state["tokens_out"],
                    cache_read_tokens=state["cache_read_tokens"],
                    cache_write_tokens=state["cache_write_tokens"],
                    cost_usd=cost,
                    status_code=state["upstream_status"] or None,
                    error_code=state["error_code"],
                    latency_ms=latency_ms,
                    client_version=request.headers.get("X-Client-Version"),
                    client_ip=request.client.host if request.client else None,
                    request_body=upstream_body,
                    response_body=response_body_text,
                    request_bytes=request_bytes,
                    response_bytes=response_bytes,
                    meta={"ttfb_ms": ttfb_ms} if ttfb_ms is not None else {},
                    chat_id=chat_id,
                )
                await session.commit()
                log.info(
                    "messages.completed",
                    status_code=state["upstream_status"],
                    latency_ms=latency_ms,
                    ttfb_ms=ttfb_ms,
                    tokens_in=state["tokens_in"],
                    tokens_out=state["tokens_out"],
                    cost_usd=str(cost) if cost is not None else None,
                )
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # Best-effort: never let a logging failure mask a stream
                # error. Rollback so the next user of this session (if
                # any — there shouldn't be any after a streaming finally)
                # doesn't pick up a dirty transaction.
                try:
                    await session.rollback()
                except Exception:  # pragma: no cover
                    pass
                log.exception("messages.request_log_insert_failed")

    headers = {
        "X-Request-Id": str(request_id),
        "Cache-Control": "no-cache",
        # Caddy doesn't buffer SSE by default but be explicit so any
        # future reverse proxy (Nginx, etc.) that respects this header
        # still streams correctly.
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers=headers,
    )


async def _messages_unary(
    body: MessagesRequest,
    request: Request,
    user: User,
    session: AsyncSession,
    settings: Settings,
    prices: dict,
) -> Response:
    """Non-streaming passthrough for callers that send ``stream: false``.

    The add-in's step labeler (``GetClaudeMessageAsync``) uses this path.
    We forward with ``stream: False``, get a JSON body back, log it, and
    return a plain JSON ``Response`` so the SDK's JSON deserialiser works.
    """
    request_id = uuid4()
    chat_id = request.headers.get("X-Chat-Id") or None
    started = time.monotonic()
    log = logger.bind(
        request_id=str(request_id),
        user_id=str(user.id),
        endpoint="messages",
        model=body.model,
    )

    upstream_body: dict[str, Any] = body.model_dump(exclude_none=True)
    upstream_body["stream"] = False

    client: httpx.AsyncClient = request.app.state.openrouter_client
    url = f"{settings.openrouter_base_url}/messages"

    cred_store: CredentialStore = request.app.state.credential_store
    try:
        or_api_key = await cred_store.resolve(SETTING_OPENROUTER_KEY, session)
    except CredentialMissing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "service_not_configured", "key": "openrouter_api_key"},
        )

    tokens_in = 0
    tokens_out = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    error_code: str | None = None
    upstream_status = 0
    response_text = ""
    response_bytes_total = 0

    try:
        resp = await client.post(url, json=upstream_body, headers=auth_headers(or_api_key))
        upstream_status = resp.status_code
        response_bytes_total = len(resp.content)
        response_text, _ = truncate(resp.text, settings.max_body_bytes)

        if resp.is_success:
            try:
                usage = resp.json().get("usage", {})
                tokens_in = usage.get("input_tokens", 0) or 0
                tokens_out = usage.get("output_tokens", 0) or 0
                cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
                cache_write_tokens = usage.get("cache_creation_input_tokens", 0) or 0
            except Exception:
                pass
        else:
            error_code = f"upstream_{resp.status_code}"

    except httpx.HTTPError as exc:
        error_code = type(exc).__name__
        upstream_status = 502
        log.warning("messages.unary.upstream_error", error_type=type(exc).__name__)
        raise HTTPException(status_code=502, detail={"error": error_code}) from exc

    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        _, req_bytes = truncate(json.dumps(upstream_body), settings.max_body_bytes)
        cost = compute_cost_usd(
            body.model,
            tokens_in,
            tokens_out,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            prices=prices,
        )
        try:
            await insert_request_log(
                session,
                request_id=request_id,
                user_id=user.id,
                endpoint="messages",
                model=body.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_usd=cost,
                status_code=upstream_status or None,
                error_code=error_code,
                latency_ms=latency_ms,
                client_version=request.headers.get("X-Client-Version"),
                client_ip=request.client.host if request.client else None,
                request_body=upstream_body,
                response_body=response_text,
                request_bytes=req_bytes,
                response_bytes=response_bytes_total,
                meta={},
                chat_id=chat_id,
            )
            await session.commit()
            log.info(
                "messages.unary.completed",
                status_code=upstream_status,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            log.exception("messages.unary.request_log_insert_failed")

    return Response(
        content=resp.content,
        status_code=upstream_status,
        media_type="application/json",
        headers={"X-Request-Id": str(request_id)},
    )
