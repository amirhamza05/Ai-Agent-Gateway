"""Pre-call gating dependencies: rate limit + monthly cost cap.

Per §9 of the plan, every authenticated upstream call must be gated
on two cheap checks BEFORE the gateway opens an outbound HTTP
connection:

1. **Rate limit** — Redis token bucket, ~30 req/min per user. Cheap
   (one EVALSHA round-trip). Returns 429 + ``Retry-After``.
2. **Monthly USD cap** — SUM(cost_usd) since ``date_trunc('month')``.
   One indexed query against ``request_log``. Returns 402.

These deps are kept out of ``auth/deps.py`` so the identity
machinery (``require_user``, ``get_db_session``) stays focused on
"who is the caller". Anything that gates *what they can do* lives
here.

Order matters. ``enforce_monthly_cap`` ``Depends(enforce_rate_limit)``
so a single ``user: User = Depends(enforce_monthly_cap)`` parameter on
a route handler runs all three checks (auth → rate-limit → cap) in
the right order. That means the cheap rate-limit check stops a
runaway client before it triggers the more expensive ledger query.
"""

from __future__ import annotations

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session, require_user
from gateway.billing import check_monthly_cap
from gateway.config import Settings, get_settings
from gateway.db.models import ApiToken, ApiTokenModel, User
from gateway.ratelimit import check_user_rate_limit

logger = structlog.get_logger(__name__)


async def enforce_rate_limit(
    request: Request,
    user: User = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> User:
    """Atomically deduct one token from the caller's per-user bucket.

    Reaches for the Redis client off ``app.state.redis`` (attached by
    the lifespan). Failure to deduct raises ``429 Too Many Requests``
    with both:

    * ``Retry-After`` header — what RFC 9110-aware clients read.
    * ``detail.retry_after_sec`` — what the add-in's structured-error
      parser keys off (we keep all error info in ``detail`` so a
      caller using ``response.json()`` doesn't need to also parse
      headers).

    Returns the original :class:`User` so this dep can chain into
    :func:`enforce_monthly_cap` — FastAPI threads the value through
    automatically.
    """
    redis = request.app.state.redis
    result = await check_user_rate_limit(redis, user.id, settings)
    if not result.allowed:
        logger.info(
            "limits.rate_limited",
            user_id=str(user.id),
            retry_after_sec=result.retry_after_sec,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limited",
                "retry_after_sec": result.retry_after_sec,
            },
            headers={"Retry-After": str(result.retry_after_sec)},
        )
    return user


async def enforce_monthly_cap(
    user: User = Depends(enforce_rate_limit),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Block the call when the caller has hit their monthly USD cap.

    Runs AFTER :func:`enforce_rate_limit` (chained through
    ``Depends``) so the cheap bucket check rejects abusive callers
    before we touch the ledger. The cap query is one COALESCE/SUM
    against ``request_log`` keyed on the ``(user_id, created_at
    DESC)`` index — a few-hundred-row scan in steady state.

    The 402 detail carries both ``cap_usd`` and ``spent_usd`` so the
    add-in can render an accurate "You've used $9.97 of your $10.00
    monthly limit" message without making a follow-up ``/v1/usage``
    call.
    """
    result = await check_monthly_cap(session, user)
    if not result.allowed:
        logger.info(
            "limits.monthly_cap_exceeded",
            user_id=str(user.id),
            spent_usd=str(result.spent),
            cap_usd=str(result.cap),
        )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "monthly_cap_exceeded",
                "cap_usd": float(result.cap),
                "spent_usd": float(result.spent),
            },
        )
    return user


async def enforce_token_model_scope(
    request: Request,
    session: AsyncSession,
    *,
    model: str,
) -> None:
    """Reject the call if the bearer JWT's underlying api_token forbids ``model``.

    Looks for ``request.state.api_token_id`` (set by ``require_user``
    when the JWT carries a ``tid`` claim — i.e. it was minted via
    ``/auth/token/connect``). When absent, this is a user-level JWT
    from ``/auth/login`` and there is no per-token restriction to
    apply. When present and ``allow_all_models=False``, the requested
    model must be in the join-table allow-list — otherwise raise 403
    ``model_not_in_token_scope`` so the add-in can surface a clear
    "this token can't use that model" message.
    """
    api_token_id = getattr(request.state, "api_token_id", None)
    if api_token_id is None:
        return

    token_row = await session.get(ApiToken, api_token_id)
    if token_row is None or not token_row.is_active:
        # The JWT outlived its api_token (revoked/deleted). Reject —
        # we can't safely reason about the scope.
        logger.info("limits.token_missing_or_inactive", token_id=str(api_token_id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    if token_row.allow_all_models:
        return

    scope_result = await session.execute(
        select(ApiTokenModel.model).where(
            ApiTokenModel.token_id == api_token_id,
            ApiTokenModel.model == model,
        )
    )
    if scope_result.scalar_one_or_none() is None:
        logger.info(
            "limits.token_model_scope_violation",
            token_id=str(api_token_id),
            model=model,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "model_not_in_token_scope",
                "model": model,
            },
        )
