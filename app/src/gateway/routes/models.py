"""``GET /v1/models`` — pricing + allow-list for the calling token.

The add-in (and any AI-agent UI) needs to know which models the user can
call and what each costs so it can render a model picker with prices.
This endpoint returns exactly that, scoped to the bearer JWT:

* If the JWT was minted via ``/auth/token/connect`` and the underlying
  ``api_token`` has ``allow_all_models=False``, only the join-table rows
  in ``api_token_models`` are returned.
* Otherwise (no token id in the JWT, or ``allow_all_models=True``) every
  currently-allowed pricing row is returned.

Disabled rows (``disabled_at IS NOT NULL``) and unallowed rows
(``is_allowed=False``) are filtered out — the response only ever lists
models the caller may actually use.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session, require_user
from gateway.db.models import ApiToken, ApiTokenModel, ModelPricing, User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models", summary="Models available to the calling token")
async def list_models(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """Return the models this bearer token may call, with pricing.

    The response shape is intentionally compact so an AI-agent UI can
    consume it directly. Per-million-token prices are returned as floats
    rounded at the database NUMERIC precision (4 dp) — callers should
    treat them as display values and not re-derive cost from them
    (the gateway computes cost authoritatively).
    """
    api_token_id = getattr(request.state, "api_token_id", None)

    allow_all_models = True
    scoped_models: set[str] | None = None

    if api_token_id is not None:
        token_row = await session.get(ApiToken, api_token_id)
        if token_row is not None and token_row.is_active:
            allow_all_models = bool(token_row.allow_all_models)
            if not allow_all_models:
                scope_result = await session.execute(
                    select(ApiTokenModel.model).where(
                        ApiTokenModel.token_id == api_token_id
                    )
                )
                scoped_models = {row[0] for row in scope_result.all()}

    pricing_cache = request.app.state.pricing_cache
    prices = await pricing_cache.get_all(session)

    items: list[dict[str, object]] = []
    for model_id, row in sorted(prices.items()):
        if not row.is_allowed:
            continue
        # The token-scope feature only governs messages models — embeddings
        # are always available to any authenticated caller and are not
        # surfaced through this endpoint, which exists so an AI-agent UI
        # can render a chat-model picker.
        if row.endpoint_kind != "messages":
            continue
        if not allow_all_models and (
            scoped_models is None or model_id not in scoped_models
        ):
            continue
        items.append(
            {
                "model": row.model,
                "endpoint_kind": row.endpoint_kind,
                "input_per_mtoken": float(row.input_per_mtoken),
                "output_per_mtoken": (
                    float(row.output_per_mtoken)
                    if row.output_per_mtoken is not None
                    else None
                ),
                "cache_read_per_mtoken": (
                    float(row.cache_read_per_mtoken)
                    if row.cache_read_per_mtoken is not None
                    else None
                ),
                "cache_write_per_mtoken": (
                    float(row.cache_write_per_mtoken)
                    if row.cache_write_per_mtoken is not None
                    else None
                ),
            }
        )

    return {
        "allow_all_models": allow_all_models,
        "count": len(items),
        "models": items,
    }
