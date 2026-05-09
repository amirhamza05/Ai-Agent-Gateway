"""``POST /v1/qdrant/search`` and ``POST /v1/qdrant/upsert`` — Qdrant proxy.

Both endpoints are JSON-in / JSON-out passthroughs to Qdrant Cloud's
points API. The collection name is user-controlled, so we validate it
against a strict regex before interpolating into the URL — see
:func:`gateway.upstream.qdrant.validate_collection`.

Cost handling: Qdrant Cloud bills on storage, not per-call. Therefore
``cost_usd`` for these rows is **always 0** (not ``NULL`` — we want the
SUM(cost_usd) query in ``/v1/usage`` to keep working without a special
case). Traffic is still logged so audits and per-endpoint usage
breakdowns work.

Logging discipline matches the messages route: one ``request_log`` row
per call written from a ``try/finally`` so failed upstream calls (4xx
from Qdrant, network error) still produce an audit row.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session
from gateway.config import Settings, get_settings
from gateway.credential_store import (
    CredentialMissing,
    CredentialStore,
    SETTING_QDRANT_KEY,
    SETTING_QDRANT_URL,
)
from gateway.db.models import User
from gateway.limits import enforce_monthly_cap
from gateway.logging_mw import insert_request_log
from gateway.truncate import truncate
from gateway.upstream.qdrant import (
    auth_headers,
    search_url,
    upsert_url,
    validate_collection,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/qdrant", tags=["qdrant"])


# Qdrant calls cost $0 in the gateway's per-request ledger (storage
# pricing is out-of-band). Pre-quantized to match the Numeric(10,6)
# column shape so SUM() keeps clean Decimal arithmetic.
_QDRANT_COST_USD = Decimal("0.000000")


# ---- Request models -------------------------------------------------------


class QdrantSearchRequest(BaseModel):
    """Search points in a Qdrant collection.

    Only a curated subset of Qdrant's full search body is exposed —
    enough for the add-in's ``SearchGeoswmmDocsTool`` and easy to extend
    if we later need ``params``, ``offset``, ``with_vectors``, etc.

    ``extra="forbid"`` so unknown fields don't sneak through. The
    gateway is the trust boundary; we'd rather break a misconfigured
    client loudly here than have it ship garbage to Qdrant.
    """

    model_config = ConfigDict(extra="forbid")

    collection: str
    # Cap on vector dim so a single request can't ship a million-element
    # array and exhaust the request size limit. 4096 is the largest
    # commonly-used embedding dim today (OpenAI 3-large is 3072).
    vector: list[float] = Field(min_length=1, max_length=4096)
    # 200 caps a single search request well below Qdrant's recommended
    # max while still being plenty for top-K retrieval workloads.
    limit: int = Field(default=10, ge=1, le=200)
    # ``filter`` is free-form per Qdrant's filter DSL — too rich to
    # mirror here. We pass it through opaquely.
    filter: dict[str, Any] | None = None
    # ``with_payload`` is either a bool (return all) or a selector dict
    # ({"include": [...], "exclude": [...]}). We accept both.
    with_payload: bool | dict[str, Any] = True
    score_threshold: float | None = None


class QdrantUpsertRequest(BaseModel):
    """Upsert (write) points into a Qdrant collection.

    The point shape — ``{"id": ..., "vector": ..., "payload": ...}`` —
    is Qdrant's, not ours. We accept it as a free-form dict and forward
    verbatim. ``points`` is capped at 512 to prevent a single call from
    pushing a multi-MB body through the gateway's request-size middleware.
    """

    model_config = ConfigDict(extra="forbid")

    collection: str
    points: list[dict[str, Any]] = Field(min_length=1, max_length=512)
    # ``wait=true`` makes the response block until the points are indexed
    # — what the add-in wants when it expects a follow-up search to find
    # them. Defaults to true for the same reason.
    wait: bool = True


# ---- Helpers --------------------------------------------------------------


async def _proxy(
    *,
    request: Request,
    user: User,
    session: AsyncSession,
    qdrant_api_key: str,
    max_body_bytes: int,
    method: str,
    url: str,
    upstream_body: dict[str, Any],
    endpoint_name: str,
    meta: dict[str, Any],
    chat_id: str | None = None,
) -> JSONResponse:
    """Shared body for the two Qdrant routes.

    Both search and upsert have the same audit/error/logging pattern;
    factoring it into one helper keeps the route handlers small and
    makes the logging contract identical.

    The Qdrant client is shared with the rest of the app via
    ``request.app.state.qdrant_client``. We never reach for the inbound
    Authorization header — only the gateway's server-side ``api-key``
    lands on the wire (see :func:`auth_headers`).
    """
    request_id = uuid4()
    started = time.monotonic()
    log = logger.bind(
        request_id=str(request_id),
        user_id=str(user.id),
        endpoint=endpoint_name,
    )

    client: httpx.AsyncClient = request.app.state.qdrant_client

    state: dict[str, Any] = {
        "status_code": 0,
        "error_code": None,
        "response_text": "",
        "response_bytes": 0,
    }

    try:
        try:
            resp = await client.request(
                method,
                url,
                json=upstream_body,
                headers=auth_headers(qdrant_api_key),
            )
        except httpx.HTTPError as exc:
            state["error_code"] = type(exc).__name__
            state["status_code"] = status.HTTP_502_BAD_GATEWAY
            log.warning("qdrant.upstream_error", error_type=type(exc).__name__)
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"error": state["error_code"]},
                headers={"X-Request-Id": str(request_id)},
            )

        state["status_code"] = resp.status_code
        state["response_bytes"] = len(resp.content)
        state["response_text"] = (
            resp.text if isinstance(resp.text, str) else resp.content.decode("utf-8", "replace")
        )

        try:
            parsed = resp.json()
        except (ValueError, httpx.DecodingError):
            parsed = None

        if resp.status_code >= 400:
            state["error_code"] = f"upstream_{resp.status_code}"
            content: Any = parsed if parsed is not None else {"error": state["error_code"]}
            return JSONResponse(
                status_code=resp.status_code,
                content=content,
                headers={"X-Request-Id": str(request_id)},
            )

        return JSONResponse(
            status_code=resp.status_code,
            content=parsed if parsed is not None else {},
            headers={"X-Request-Id": str(request_id)},
        )
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        max_bytes = max_body_bytes

        request_body_text, request_bytes = truncate(
            json.dumps(upstream_body), max_bytes
        )
        response_body_text, _ = truncate(state["response_text"] or "", max_bytes)
        response_bytes = state["response_bytes"] or len(
            (state["response_text"] or "").encode("utf-8")
        )

        try:
            await insert_request_log(
                session,
                request_id=request_id,
                user_id=user.id,
                endpoint=endpoint_name,
                model=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=_QDRANT_COST_USD,
                status_code=state["status_code"] or None,
                error_code=state["error_code"],
                latency_ms=latency_ms,
                client_version=request.headers.get("X-Client-Version"),
                client_ip=request.client.host if request.client else None,
                request_body=upstream_body,
                response_body=response_body_text,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                meta=meta,
                chat_id=chat_id,
            )
            await session.commit()
            log.info(
                "qdrant.completed",
                status_code=state["status_code"],
                latency_ms=latency_ms,
            )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:  # pragma: no cover
                pass
            log.exception("qdrant.request_log_insert_failed")


# ---- Routes ---------------------------------------------------------------


@router.post("/search", summary="Search points in a Qdrant collection")
async def qdrant_search(
    body: QdrantSearchRequest,
    request: Request,
    user: User = Depends(enforce_monthly_cap),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Forward a points-search to Qdrant.

    Cost: 0.00 USD recorded in ``request_log.cost_usd``. Qdrant Cloud
    bills on storage, not per-call.
    """
    # Validates against ``^[A-Za-z0-9_\\-]{1,64}$``. Raises 400 if not
    # matched — the URL is then safe to interpolate.
    validate_collection(body.collection)

    cred_store: CredentialStore = request.app.state.credential_store
    try:
        qdrant_key = await cred_store.resolve(SETTING_QDRANT_KEY, session)
        q_url = await cred_store.resolve(SETTING_QDRANT_URL, session)
    except CredentialMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "service_not_configured", "key": str(exc)},
        )

    # Build the upstream body exclude-None style. Qdrant rejects unknown
    # fields strictly, and a nullable ``filter`` would be sent as
    # ``"filter": null`` which Qdrant interprets as "filter present but
    # empty" on some versions.
    upstream_body: dict[str, Any] = {
        "vector": body.vector,
        "limit": body.limit,
        "with_payload": body.with_payload,
    }
    if body.filter is not None:
        upstream_body["filter"] = body.filter
    if body.score_threshold is not None:
        upstream_body["score_threshold"] = body.score_threshold

    return await _proxy(
        request=request,
        user=user,
        session=session,
        qdrant_api_key=qdrant_key,
        max_body_bytes=settings.max_body_bytes,
        method="POST",
        url=search_url(q_url, body.collection),
        upstream_body=upstream_body,
        endpoint_name="qdrant.search",
        meta={"collection": body.collection, "limit": body.limit},
        chat_id=request.headers.get("X-Chat-Id") or None,
    )


@router.post("/upsert", summary="Upsert points into a Qdrant collection")
async def qdrant_upsert(
    body: QdrantUpsertRequest,
    request: Request,
    user: User = Depends(enforce_monthly_cap),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Forward a points-upsert (PUT) to Qdrant.

    Cost: 0.00 USD recorded in ``request_log.cost_usd``. Qdrant Cloud
    bills on storage, not per-call.
    """
    validate_collection(body.collection)

    cred_store: CredentialStore = request.app.state.credential_store
    try:
        qdrant_key = await cred_store.resolve(SETTING_QDRANT_KEY, session)
        q_url = await cred_store.resolve(SETTING_QDRANT_URL, session)
    except CredentialMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "service_not_configured", "key": str(exc)},
        )

    upstream_body: dict[str, Any] = {"points": body.points}

    return await _proxy(
        request=request,
        user=user,
        session=session,
        qdrant_api_key=qdrant_key,
        max_body_bytes=settings.max_body_bytes,
        method="PUT",
        url=upsert_url(q_url, body.collection, wait=body.wait),
        upstream_body=upstream_body,
        endpoint_name="qdrant.upsert",
        meta={
            "collection": body.collection,
            "point_count": len(body.points),
        },
        chat_id=request.headers.get("X-Chat-Id") or None,
    )
