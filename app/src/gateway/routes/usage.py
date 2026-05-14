"""``GET /v1/usage`` — current month's spend snapshot.

Phase 3 swapped the placeholder zeros for a real aggregate query against
``request_log`` (per §9 of the plan). The query is window-by-month using
``date_trunc('month', now())`` so a user's billing window aligns with
the calendar month, matching the cap semantics.

Phase 5 adds an ``endpoints`` breakdown so callers can see how spend
distributes across ``messages``, ``embeddings``, ``vectors.search``, and
``vectors.upsert``. The breakdown is a second indexed query over the same
predicate; we avoid a single ``GROUPING SETS`` query because two simple
``GROUP BY`` queries are easier to reason about and the difference is
sub-millisecond on the indexed read path.

The top-level fields stay the same as P3 — the add-in only reads
``spent_usd`` and ``request_count`` today and ignores the rest, so the
``endpoints`` dict is purely additive.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session, require_user
from gateway.db.models import RequestLog, User

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage", summary="Current month's usage for the calling user")
async def get_usage(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """Return the calling user's monthly cap and current consumption.

    Aggregates ``request_log`` rows for the calling user from the start
    of the current calendar month through ``now()``. ``COALESCE(.., 0)``
    keeps the response shape stable when the user hasn't made any
    requests yet (NULL aggregates would deserialise as ``null``).

    The response carries:

    * Top-level totals — ``spent_usd``, ``request_count``, ``tokens_in``,
      ``tokens_out`` summed across every endpoint.
    * ``endpoints`` — a per-endpoint dict (``messages``, ``embeddings``,
      ``vectors.search``, ``vectors.upsert``, ...) with the same shape so
      the add-in can render a breakdown without computing it client-side.
    """
    period_start_expr = func.date_trunc("month", func.now())

    # Top-level aggregate (unchanged from P3).
    totals_stmt = select(
        func.coalesce(func.sum(RequestLog.cost_usd), 0).label("spent_usd"),
        func.count().label("request_count"),
        func.coalesce(func.sum(RequestLog.tokens_in), 0).label("tokens_in"),
        func.coalesce(func.sum(RequestLog.tokens_out), 0).label("tokens_out"),
        period_start_expr.label("period_start"),
    ).where(
        RequestLog.user_id == user.id,
        RequestLog.created_at >= period_start_expr,
    )
    totals_row = (await session.execute(totals_stmt)).one()

    # P5 addition: per-endpoint breakdown. Same predicate as the top
    # query so it hits the same ``(user_id, created_at DESC)`` index.
    by_endpoint_stmt = (
        select(
            RequestLog.endpoint,
            func.coalesce(func.sum(RequestLog.cost_usd), 0).label("spent_usd"),
            func.count().label("requests"),
            func.coalesce(func.sum(RequestLog.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(RequestLog.tokens_out), 0).label("tokens_out"),
        )
        .where(
            RequestLog.user_id == user.id,
            RequestLog.created_at >= period_start_expr,
        )
        .group_by(RequestLog.endpoint)
    )
    endpoints: dict[str, dict[str, float | int]] = {}
    for row in (await session.execute(by_endpoint_stmt)).all():
        endpoints[row.endpoint] = {
            "requests": int(row.requests),
            "spent_usd": float(row.spent_usd),
            "tokens_in": int(row.tokens_in),
            "tokens_out": int(row.tokens_out),
        }

    period_start: datetime = totals_row.period_start
    return {
        "user_id": str(user.id),
        "monthly_usd_cap": float(user.monthly_usd_cap),
        "spent_usd": float(totals_row.spent_usd),
        "request_count": int(totals_row.request_count),
        "tokens_in": int(totals_row.tokens_in),
        "tokens_out": int(totals_row.tokens_out),
        "period_start": period_start.isoformat(),
        "endpoints": endpoints,
    }
