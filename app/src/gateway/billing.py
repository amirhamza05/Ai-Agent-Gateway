"""Cost computation and monthly-cap check.

Per §9 of the plan: cost is computed from a small price table keyed by
the model ID exactly as it appears in ``model_pricing.model``. Prices
are USD per **million** tokens (the common Anthropic / OpenRouter unit).

Phase D moves pricing out of in-process constants into the
``model_pricing`` DB table so operators can edit pricing and the model
allow-list from the dashboard without a deploy. The :class:`PricingCache`
keeps a 30-second TTL snapshot in memory so the cost-on-write hot path
in ``/v1/messages`` and ``/v1/embeddings`` doesn't run an extra
SELECT on every request. Dashboard mutations call ``cache.invalidate()``
on commit so changes are visible within a few hundred milliseconds.

The legacy module-level dicts (:data:`PRICES_PER_MTOKEN` and
:data:`EMBEDDING_PRICES_PER_MTOKEN`) are kept as a **bootstrap fallback**:
when the DB pricing table is empty (a fresh deploy before the migration
seed has been applied, or a developer has hand-truncated it), the
gateway falls back to these constants and logs a critical warning at
startup. They also remain useful for unit tests that don't want to
spin up a Postgres container.

Math is done with :class:`decimal.Decimal` so the per-row value carries no
binary-float artefacts.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import func, select

from gateway.db.models import ModelPricing, RequestLog

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

    from gateway.db.models import User

logger = structlog.get_logger(__name__)

# ---- Legacy / bootstrap constants ------------------------------------
#
# Kept for two reasons:
#
# 1. **Bootstrap fallback** — if ``model_pricing`` is empty (a fresh
#    deploy before the migration seed has been applied, or a developer
#    has hand-truncated it) the gateway falls back to these so v1
#    traffic doesn't hard-fail. A critical warning is logged at startup
#    so the operator notices.
# 2. **Unit tests** — ``test_billing`` checks the math in isolation,
#    without spinning up a Postgres container.
#
# These constants are NO LONGER the source of truth for live traffic —
# the ``model_pricing`` table is. Update both when adding a new model
# until the dashboard is the only edit path.

# USD per million tokens. Keys MUST match the ``model_pricing.model`` PK.
PRICES_PER_MTOKEN: dict[str, dict[str, float]] = {
    "anthropic/claude-opus-4.7":   {"in": 15.00, "out": 75.00},
    "anthropic/claude-sonnet-4.6": {"in":  3.00, "out": 15.00},
    "anthropic/claude-haiku-4.5":  {"in":  1.00, "out":  5.00},
}

# USD per million input tokens. Embeddings have no "output" tokens.
EMBEDDING_PRICES_PER_MTOKEN: dict[str, float] = {
    "openai/text-embedding-3-small": 0.020,
    "openai/text-embedding-3-large": 0.130,
}

# Numeric(10, 6) in the DB → quantize to 6 decimals at the boundary.
_COST_QUANT = Decimal("0.000001")
_PER_MTOKEN = Decimal(1_000_000)


# ---- Pricing snapshot + TTL cache ------------------------------------


@dataclass(frozen=True)
class _PriceRow:
    """One pricing row, normalised to ``Decimal``."""

    model: str
    endpoint_kind: str
    input_per_mtoken: Decimal
    output_per_mtoken: Decimal | None
    is_allowed: bool


class PricingCache:
    """30-second in-process TTL cache for the ``model_pricing`` table.

    Single-process per worker — the gateway runs at most a handful of
    uvicorn workers and each carries its own cache. There's no need for
    cross-process pub/sub: the worst case after a dashboard write is a
    single 30-second window where one worker hasn't yet seen the
    update, and dashboard handlers call ``invalidate()`` on the same
    worker that handled the form post.

    Concurrent first-fill is serialised by an :class:`asyncio.Lock` so
    a burst of /v1/messages requests at startup doesn't fan out into
    many parallel SELECTs against ``model_pricing``.
    """

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._snapshot: dict[str, _PriceRow] | None = None
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_all(self, session: AsyncSession) -> dict[str, _PriceRow]:
        """Return ``{model: _PriceRow}`` from cache or DB.

        Cache is refreshed when the TTL has elapsed since the last load.
        Soft-deleted rows (``disabled_at IS NOT NULL``) are excluded
        from the snapshot so the v1 routes never pick them up.
        """
        now = time.monotonic()
        if (
            self._snapshot is not None
            and (now - self._loaded_at) < self._ttl
        ):
            return self._snapshot

        async with self._lock:
            # Re-check after taking the lock to avoid duplicate loads
            # under contention.
            now = time.monotonic()
            if (
                self._snapshot is not None
                and (now - self._loaded_at) < self._ttl
            ):
                return self._snapshot

            stmt = select(ModelPricing).where(ModelPricing.disabled_at.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            snapshot: dict[str, _PriceRow] = {
                row.model: _PriceRow(
                    model=row.model,
                    endpoint_kind=row.endpoint_kind,
                    input_per_mtoken=Decimal(row.input_per_mtoken),
                    output_per_mtoken=(
                        Decimal(row.output_per_mtoken)
                        if row.output_per_mtoken is not None
                        else None
                    ),
                    is_allowed=bool(row.is_allowed),
                )
                for row in rows
            }

            if not snapshot:
                # Bootstrap fallback. The startup warning lets the operator
                # know they're running on legacy in-process pricing.
                logger.warning(
                    "billing.pricing_table_empty_falling_back_to_constants"
                )
                for model, prices in PRICES_PER_MTOKEN.items():
                    snapshot[model] = _PriceRow(
                        model=model,
                        endpoint_kind="messages",
                        input_per_mtoken=Decimal(str(prices["in"])),
                        output_per_mtoken=Decimal(str(prices["out"])),
                        is_allowed=True,
                    )
                for model, price in EMBEDDING_PRICES_PER_MTOKEN.items():
                    snapshot[model] = _PriceRow(
                        model=model,
                        endpoint_kind="embeddings",
                        input_per_mtoken=Decimal(str(price)),
                        output_per_mtoken=None,
                        is_allowed=True,
                    )

            self._snapshot = snapshot
            self._loaded_at = now
            return snapshot

    def invalidate(self) -> None:
        """Drop the cached snapshot so the next ``get_all`` re-reads.

        Called by every dashboard handler that mutates ``model_pricing``
        (insert, update, delete) immediately after the commit returns.
        """
        self._snapshot = None
        self._loaded_at = 0.0


# ---- Cost helpers ----------------------------------------------------


def is_model_allowed(model: str, *, prices: dict[str, _PriceRow]) -> bool:
    """Return True iff ``model`` is in ``prices`` and ``is_allowed=True``.

    Replaces the legacy ``model in settings.allowed_models_set`` check
    in the v1 routes. Soft-deleted rows are already excluded by
    :meth:`PricingCache.get_all`, so a missing key here means the model
    isn't priced.
    """
    row = prices.get(model)
    if row is None:
        return False
    return row.is_allowed


def compute_cost_usd(
    model: str,
    tokens_in: int,
    tokens_out: int,
    *,
    prices: dict[str, _PriceRow] | None = None,
) -> Decimal | None:
    """Return cost in USD for ``model``, quantized to 6 decimals.

    Returns ``None`` when the model isn't priced (caller logs a warning
    so we notice an unpriced model in production without breaking the
    request). Returns ``Decimal("0")`` when both token counts are zero.

    ``prices`` is the snapshot returned by
    :meth:`PricingCache.get_all`. When omitted (unit tests, ad-hoc
    invocations) the function falls back to the legacy in-process
    :data:`PRICES_PER_MTOKEN` dict so existing callers keep working.
    """
    price_in: Decimal
    price_out: Decimal

    if prices is not None:
        row = prices.get(model)
        if row is None or row.endpoint_kind != "messages":
            logger.warning("billing.unpriced_model", model=model)
            return None
        price_in = row.input_per_mtoken
        price_out = (
            row.output_per_mtoken
            if row.output_per_mtoken is not None
            else Decimal(0)
        )
    else:
        legacy = PRICES_PER_MTOKEN.get(model)
        if legacy is None:
            logger.warning("billing.unpriced_model", model=model)
            return None
        price_in = Decimal(str(legacy["in"]))
        price_out = Decimal(str(legacy["out"]))

    cost = (
        Decimal(int(tokens_in)) * price_in
        + Decimal(int(tokens_out)) * price_out
    ) / _PER_MTOKEN

    return cost.quantize(_COST_QUANT, rounding=ROUND_HALF_EVEN)


def compute_embedding_cost_usd(
    model: str,
    tokens: int,
    *,
    prices: dict[str, _PriceRow] | None = None,
) -> Decimal | None:
    """Return embedding cost in USD for ``model`` and ``tokens``.

    Returns ``None`` when the model isn't priced. Returns
    ``Decimal("0")`` when ``tokens == 0``. Same Decimal/ROUND_HALF_EVEN
    /6-decimal-quantize discipline as :func:`compute_cost_usd`.

    ``prices`` parameter has the same fallback semantics.
    """
    price_dec: Decimal

    if prices is not None:
        row = prices.get(model)
        if row is None or row.endpoint_kind != "embeddings":
            logger.warning("billing.unpriced_embedding_model", model=model)
            return None
        price_dec = row.input_per_mtoken
    else:
        legacy = EMBEDDING_PRICES_PER_MTOKEN.get(model)
        if legacy is None:
            logger.warning("billing.unpriced_embedding_model", model=model)
            return None
        price_dec = Decimal(str(legacy))

    cost = (Decimal(int(tokens)) * price_dec) / _PER_MTOKEN
    return cost.quantize(_COST_QUANT, rounding=ROUND_HALF_EVEN)


# ---- Monthly cap (P4) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapResult:
    """Result of a monthly-cap check."""

    allowed: bool
    spent: Decimal
    cap: Decimal
    remaining: Decimal


async def get_month_to_date_cost(
    session: AsyncSession,
    user_id: UUID,
) -> Decimal:
    """Return the user's total ``cost_usd`` since the start of this month."""
    period_start = func.date_trunc("month", func.now())
    stmt = (
        select(func.coalesce(func.sum(RequestLog.cost_usd), 0))
        .where(
            RequestLog.user_id == user_id,
            RequestLog.created_at >= period_start,
        )
    )
    result = await session.execute(stmt)
    raw = result.scalar_one()
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw))


async def check_monthly_cap(
    session: AsyncSession,
    user: User,
) -> CapResult:
    """Pre-call check: would this request push the user past their cap?"""
    cap = Decimal(user.monthly_usd_cap)
    spent = await get_month_to_date_cost(session, user.id)

    if cap <= 0:
        return CapResult(allowed=True, spent=spent, cap=cap, remaining=cap)

    remaining = cap - spent
    if remaining < 0:
        remaining = Decimal(0)
    return CapResult(
        allowed=spent < cap,
        spent=spent,
        cap=cap,
        remaining=remaining,
    )
