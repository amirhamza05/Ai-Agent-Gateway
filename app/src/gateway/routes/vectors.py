"""``POST /v1/vectors/search`` and ``POST /v1/vectors/upsert`` — pgvector proxy.

Both endpoints are JSON-in / JSON-out passthroughs to the local pgvector
backend (:mod:`gateway.upstream.pgvector`).

Cost handling: vector calls do not bill per-call. ``cost_usd`` for these rows
is always 0 (not ``NULL`` — we want the SUM(cost_usd) query in ``/v1/usage``
to keep working without a special case). Traffic is still logged so audits
and per-endpoint usage breakdowns work.

Logging discipline matches the messages route: one ``request_log`` row per
call written from a ``try/finally`` so failed calls still produce an audit
row.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session
from gateway.config import Settings, get_settings
from gateway.db.models import User
from gateway.limits import enforce_monthly_cap
from gateway.logging_mw import insert_request_log
from gateway.truncate import truncate
from gateway.upstream import pgvector as pgvector_upstream
from gateway.upstream.pgvector import validate_collection

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/vectors", tags=["vectors"])


# Pre-quantized to match the Numeric(10,6) column shape so SUM() keeps
# clean Decimal arithmetic.
_VECTORS_COST_USD = Decimal("0.000000")


# ---- Request models -------------------------------------------------------


class VectorSearchRequest(BaseModel):
    """Search points in a vector collection."""

    model_config = ConfigDict(extra="forbid")

    collection: str
    # Cap on vector dim so a single request can't ship a million-element
    # array and exhaust the request size limit. 4096 is the largest
    # commonly-used embedding dim today (OpenAI 3-large is 3072).
    vector: list[float] = Field(min_length=1, max_length=4096)
    limit: int = Field(default=10, ge=1, le=200)
    filter: dict[str, Any] | None = None
    with_payload: bool | dict[str, Any] = True
    score_threshold: float | None = None


class VectorUpsertRequest(BaseModel):
    """Upsert (write) points into a vector collection."""

    model_config = ConfigDict(extra="forbid")

    collection: str
    points: list[dict[str, Any]] = Field(min_length=1, max_length=512)
    wait: bool = True


# ---- Logging helper -------------------------------------------------------


async def _write_request_log(
    *,
    session: AsyncSession,
    request: Request,
    request_id: object,
    user: User,
    endpoint_name: str,
    upstream_body: dict[str, Any],
    response_text: str,
    response_bytes: int,
    status_code: int | None,
    error_code: str | None,
    latency_ms: int,
    meta: dict[str, Any],
    max_body_bytes: int,
    chat_id: str | None,
    log: object,
) -> None:
    """Write one ``request_log`` row and commit."""
    request_body_text, request_bytes = truncate(
        json.dumps(upstream_body), max_body_bytes
    )
    response_body_text, _ = truncate(response_text or "", max_body_bytes)
    actual_response_bytes = response_bytes or len((response_text or "").encode("utf-8"))

    try:
        await insert_request_log(
            session,
            request_id=request_id,  # type: ignore[arg-type]
            user_id=user.id,
            endpoint=endpoint_name,
            model=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=_VECTORS_COST_USD,
            status_code=status_code,
            error_code=error_code,
            latency_ms=latency_ms,
            client_version=request.headers.get("X-Client-Version"),
            client_ip=request.client.host if request.client else None,
            request_body=upstream_body,
            response_body=response_body_text,
            request_bytes=request_bytes,
            response_bytes=actual_response_bytes,
            meta=meta,
            chat_id=chat_id,
        )
        await session.commit()
        log.info(  # type: ignore[union-attr]
            "vectors.completed",
            status_code=status_code,
            latency_ms=latency_ms,
        )
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        try:
            await session.rollback()
        except Exception:  # pragma: no cover
            pass
        log.exception("vectors.request_log_insert_failed")  # type: ignore[union-attr]


# ---- Routes ---------------------------------------------------------------


@router.post("/search", summary="Search points in a vector collection")
async def vector_search(
    body: VectorSearchRequest,
    request: Request,
    user: User = Depends(enforce_monthly_cap),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Search the local pgvector backend.

    Cost: 0.00 USD recorded in ``request_log.cost_usd``.
    """
    validate_collection(body.collection)

    chat_id = request.headers.get("X-Chat-Id") or None

    request_id = uuid4()
    started = time.monotonic()
    log = logger.bind(
        request_id=str(request_id),
        user_id=str(user.id),
        endpoint="vectors.search",
    )

    upstream_body: dict[str, Any] = {
        "vector": body.vector,
        "limit": body.limit,
        "with_payload": body.with_payload,
    }
    if body.filter is not None:
        upstream_body["filter"] = body.filter
    if body.score_threshold is not None:
        upstream_body["score_threshold"] = body.score_threshold

    state: dict[str, Any] = {
        "status_code": None,
        "error_code": None,
        "response_text": "",
        "response_bytes": 0,
    }

    try:
        try:
            result = await pgvector_upstream.search(
                session=session,
                collection=body.collection,
                vector=body.vector,
                limit=body.limit,
                filter=body.filter,
                with_payload=body.with_payload,
                score_threshold=body.score_threshold,
            )
        except ValueError as exc:
            error_str = str(exc)
            state["status_code"] = status.HTTP_400_BAD_REQUEST
            state["error_code"] = error_str
            state["response_text"] = json.dumps({"error": error_str})
            state["response_bytes"] = len(state["response_text"].encode())
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": error_str},
                headers={"X-Request-Id": str(request_id)},
            )
        except Exception as exc:
            state["status_code"] = status.HTTP_500_INTERNAL_SERVER_ERROR
            state["error_code"] = type(exc).__name__
            log.exception("vectors.search_failed")
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "internal_error"},
                headers={"X-Request-Id": str(request_id)},
            )

        state["status_code"] = status.HTTP_200_OK
        state["response_text"] = json.dumps(result)
        state["response_bytes"] = len(state["response_text"].encode())
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=result,
            headers={"X-Request-Id": str(request_id)},
        )

    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        await _write_request_log(
            session=session,
            request=request,
            request_id=request_id,
            user=user,
            endpoint_name="vectors.search",
            upstream_body=upstream_body,
            response_text=state["response_text"],
            response_bytes=state["response_bytes"],
            status_code=state["status_code"],
            error_code=state["error_code"],
            latency_ms=latency_ms,
            meta={
                "collection": body.collection,
                "limit": body.limit,
            },
            max_body_bytes=settings.max_body_bytes,
            chat_id=chat_id,
            log=log,
        )


@router.post("/upsert", summary="Upsert points into a vector collection")
async def vector_upsert(
    body: VectorUpsertRequest,
    request: Request,
    user: User = Depends(enforce_monthly_cap),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Upsert into the local pgvector backend.

    Cost: 0.00 USD recorded in ``request_log.cost_usd``.
    """
    validate_collection(body.collection)

    chat_id = request.headers.get("X-Chat-Id") or None

    request_id = uuid4()
    started = time.monotonic()
    log = logger.bind(
        request_id=str(request_id),
        user_id=str(user.id),
        endpoint="vectors.upsert",
    )

    upstream_body: dict[str, Any] = {"points": body.points}

    state: dict[str, Any] = {
        "status_code": None,
        "error_code": None,
        "response_text": "",
        "response_bytes": 0,
    }

    try:
        try:
            result = await pgvector_upstream.upsert(
                session=session,
                collection=body.collection,
                points=body.points,
            )
        except ValueError as exc:
            error_str = str(exc)
            state["status_code"] = status.HTTP_400_BAD_REQUEST
            state["error_code"] = error_str
            state["response_text"] = json.dumps({"error": error_str})
            state["response_bytes"] = len(state["response_text"].encode())
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": error_str},
                headers={"X-Request-Id": str(request_id)},
            )
        except Exception as exc:
            state["status_code"] = status.HTTP_500_INTERNAL_SERVER_ERROR
            state["error_code"] = type(exc).__name__
            log.exception("vectors.upsert_failed")
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "internal_error"},
                headers={"X-Request-Id": str(request_id)},
            )

        state["status_code"] = status.HTTP_200_OK
        state["response_text"] = json.dumps(result)
        state["response_bytes"] = len(state["response_text"].encode())
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=result,
            headers={"X-Request-Id": str(request_id)},
        )

    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        await _write_request_log(
            session=session,
            request=request,
            request_id=request_id,
            user=user,
            endpoint_name="vectors.upsert",
            upstream_body=upstream_body,
            response_text=state["response_text"],
            response_bytes=state["response_bytes"],
            status_code=state["status_code"],
            error_code=state["error_code"],
            latency_ms=latency_ms,
            meta={
                "collection": body.collection,
                "point_count": len(body.points),
            },
            max_body_bytes=settings.max_body_bytes,
            chat_id=chat_id,
            log=log,
        )
