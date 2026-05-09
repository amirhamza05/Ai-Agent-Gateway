"""Aggregation queries that drive the dashboard's report pages.

Each function returns a list of small dataclasses ready for JSON
dumping. The HTML report pages each render a Chart.js scaffold that
fetches the matching ``.json`` endpoint client-side; the JSON
endpoints in turn call these functions.

All queries hit the ``request_log`` indexes
(``(user_id, created_at DESC)`` and ``(created_at)``) so they remain
cheap as the table grows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class CostPoint:
    day: str
    cost_usd: float


@dataclass(frozen=True, slots=True)
class TopUser:
    email: str
    spent_usd: float
    request_count: int


@dataclass(frozen=True, slots=True)
class ErrorPoint:
    bucket: str
    errors: int
    total: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class LatencyPoint:
    endpoint: str
    p50: float | None
    p95: float | None
    p99: float | None
    n: int


def _to_iso(dt: Any) -> str:
    """Return an ISO-8601 string for a datetime returned by the driver."""
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _to_float(v: Any) -> float:
    """Coerce a Decimal/None/int into a float (None → 0.0)."""
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def _interval_for_range(window: str) -> str:
    """Map a UI range string ``24h|7d|30d`` to a Postgres interval literal.

    Falls back to ``7 days`` for any other input — defensive against
    a hand-typed querystring.
    """
    return {
        "24h": "24 hours",
        "7d": "7 days",
        "30d": "30 days",
    }.get(window, "7 days")


def _bucket_for_range(window: str) -> str:
    """Pick a sensible date_trunc bucket for a given UI range."""
    return {
        "24h": "hour",
        "7d": "day",
        "30d": "day",
    }.get(window, "day")


async def cost_over_time(
    session: AsyncSession,
    *,
    window: str = "7d",
) -> list[CostPoint]:
    """Cost summed by day (or hour, for 24h) over the chosen window."""
    interval = _interval_for_range(window)
    bucket = _bucket_for_range(window)
    stmt = text(
        f"""
        SELECT date_trunc('{bucket}', created_at) AS bucket,
               COALESCE(SUM(cost_usd), 0) AS cost_usd
        FROM request_log
        WHERE created_at >= now() - interval '{interval}'
        GROUP BY bucket
        ORDER BY bucket
        """
    )
    result = await session.execute(stmt)
    rows = result.all()
    return [
        CostPoint(day=_to_iso(r.bucket), cost_usd=_to_float(r.cost_usd))
        for r in rows
    ]


async def top_users(
    session: AsyncSession,
    *,
    window: str = "30d",
    limit: int = 25,
) -> list[TopUser]:
    """Top users by spend over the chosen window."""
    interval = _interval_for_range(window)
    stmt = text(
        f"""
        SELECT u.email                                    AS email,
               COALESCE(SUM(rl.cost_usd), 0)              AS spent_usd,
               COUNT(rl.id)                               AS request_count
        FROM users u
        LEFT JOIN request_log rl ON rl.user_id = u.id
             AND rl.created_at >= now() - interval '{interval}'
        GROUP BY u.id, u.email
        ORDER BY spent_usd DESC NULLS LAST
        LIMIT :limit
        """
    )
    result = await session.execute(stmt, {"limit": limit})
    return [
        TopUser(
            email=r.email,
            spent_usd=_to_float(r.spent_usd),
            request_count=int(r.request_count or 0),
        )
        for r in result.all()
    ]


async def errors_over_time(
    session: AsyncSession,
    *,
    window: str = "7d",
) -> list[ErrorPoint]:
    """Error count per bucket, optionally split by error_code."""
    interval = _interval_for_range(window)
    bucket = _bucket_for_range(window)
    stmt = text(
        f"""
        SELECT date_trunc('{bucket}', created_at) AS bucket,
               COUNT(*) FILTER (WHERE status_code >= 400) AS errors,
               COUNT(*) AS total,
               error_code
        FROM request_log
        WHERE created_at >= now() - interval '{interval}'
        GROUP BY bucket, error_code
        ORDER BY bucket
        """
    )
    result = await session.execute(stmt)
    return [
        ErrorPoint(
            bucket=_to_iso(r.bucket),
            errors=int(r.errors or 0),
            total=int(r.total or 0),
            error_code=r.error_code,
        )
        for r in result.all()
    ]


async def latency_percentiles(
    session: AsyncSession,
    *,
    window: str = "24h",
) -> list[LatencyPoint]:
    """p50/p95/p99 latency per endpoint over the chosen window."""
    interval = _interval_for_range(window)
    stmt = text(
        f"""
        SELECT endpoint,
               percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
               percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99,
               COUNT(*) AS n
        FROM request_log
        WHERE created_at >= now() - interval '{interval}'
          AND latency_ms IS NOT NULL
        GROUP BY endpoint
        ORDER BY endpoint
        """
    )
    result = await session.execute(stmt)
    return [
        LatencyPoint(
            endpoint=r.endpoint,
            p50=_to_float(r.p50) if r.p50 is not None else None,
            p95=_to_float(r.p95) if r.p95 is not None else None,
            p99=_to_float(r.p99) if r.p99 is not None else None,
            n=int(r.n or 0),
        )
        for r in result.all()
    ]


def to_json(rows: list[Any]) -> list[dict[str, Any]]:
    """Convert a list of dataclass instances to JSON-ready dicts."""
    return [asdict(r) for r in rows]
