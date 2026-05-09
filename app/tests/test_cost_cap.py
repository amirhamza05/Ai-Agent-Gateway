"""Tests for the monthly USD cap (gateway.billing + gateway.limits).

Mirrors the structure of ``test_messages_stream.py``: real Postgres
via ``db_engine``, mocked OpenRouter via ``respx``, full e2e through
the FastAPI app via ``auth_client``.

Skipped wholesale when ``TEST_DATABASE_URL`` isn't set (the
``db_engine`` fixture handles that).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from gateway.billing import check_monthly_cap, get_month_to_date_cost
from gateway.db.models import RequestLog, User


# ---- Helpers --------------------------------------------------------------


def _insert_log(
    user_id: UUID,
    cost: Decimal,
    *,
    created_at: datetime | None = None,
) -> RequestLog:
    """Build a RequestLog row with sensible defaults for cap tests."""
    kwargs: dict[str, Any] = dict(
        request_id=uuid4(),
        user_id=user_id,
        endpoint="messages",
        model="anthropic/claude-haiku-4.5",
        tokens_in=100,
        tokens_out=50,
        cost_usd=cost,
        status_code=200,
        latency_ms=100,
        request_body={"x": 1},
        response_body="ok",
        request_bytes=10,
        response_bytes=2,
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    return RequestLog(**kwargs)


# ---- billing.check_monthly_cap (unit-ish, real DB) -----------------------


async def test_cap_with_no_history_allows(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """A fresh user with zero request_log rows is always under cap."""
    _client, _headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        spent = await get_month_to_date_cost(session, user_id)
        assert spent == Decimal(0)

        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        result = await check_monthly_cap(session, user)
    assert result.allowed is True
    assert result.spent == Decimal(0)
    assert result.remaining == result.cap


async def test_cap_allows_when_under(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """Cap=10.00, spent=2.50 → allowed, remaining=7.50."""
    _client, _headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        # Set cap explicitly so the test is independent of DEFAULT_MONTHLY_USD_CAP.
        await session.execute(
            update(User).where(User.id == user_id).values(monthly_usd_cap=Decimal("10.00"))
        )
        session.add(_insert_log(user_id, Decimal("2.500000")))
        await session.commit()

        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        result = await check_monthly_cap(session, user)
    assert result.allowed is True
    assert result.spent == Decimal("2.500000")
    assert result.cap == Decimal("10.0000")
    assert result.remaining == Decimal("7.5000")


async def test_cap_blocks_when_at_cap(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """spent >= cap → not allowed."""
    _client, _headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(monthly_usd_cap=Decimal("1.00"))
        )
        # Two rows summing to exactly 1.00.
        session.add(_insert_log(user_id, Decimal("0.600000")))
        session.add(_insert_log(user_id, Decimal("0.400000")))
        await session.commit()

        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        result = await check_monthly_cap(session, user)
    assert result.allowed is False
    assert result.spent == Decimal("1.000000")
    assert result.remaining == Decimal(0)


async def test_cap_only_counts_current_month(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """A row created last month must NOT count toward this month's spend."""
    _client, _headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    # Compute "first day of current month minus one day" via SQL so we
    # don't have to reason about Python date math vs Postgres timezones.
    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(monthly_usd_cap=Decimal("10.00"))
        )

        # Last-month row: expensive. Must be excluded from the sum.
        last_month = (
            await session.execute(
                text("SELECT date_trunc('month', now()) - interval '1 day'")
            )
        ).scalar_one()
        assert isinstance(last_month, datetime)
        session.add(_insert_log(user_id, Decimal("9.000000"), created_at=last_month))

        # Current-month row: cheap.
        session.add(_insert_log(user_id, Decimal("0.500000")))
        await session.commit()

        spent = await get_month_to_date_cost(session, user_id)
        # Only the current-month row counts.
        assert spent == Decimal("0.500000")

        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        result = await check_monthly_cap(session, user)
    assert result.allowed is True
    assert result.spent == Decimal("0.500000")


async def test_cap_zero_treated_as_unlimited(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """``monthly_usd_cap = 0`` must NOT lock the user out.

    The plan defaults to $10 and the User table has a server default,
    but a DBA editing rows could plausibly set 0. We don't want a
    misconfiguration to silently brick a customer.
    """
    _client, _headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(monthly_usd_cap=Decimal("0.0000"))
        )
        # A row with positive cost — would exceed if 0 were enforced.
        session.add(_insert_log(user_id, Decimal("0.001000")))
        await session.commit()

        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        result = await check_monthly_cap(session, user)
    assert result.allowed is True


# ---- End-to-end via /v1/messages -----------------------------------------


async def test_messages_endpoint_returns_402_when_capped(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """Set cap=0.01, insert one expensive row, next /v1/messages → 402."""
    client, headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(monthly_usd_cap=Decimal("0.01"))
        )
        # One row of $0.05 — well past the $0.01 cap. The cap-check is
        # inclusive at the boundary so this triggers 402 next call.
        session.add(_insert_log(user_id, Decimal("0.050000")))
        await session.commit()

    resp = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "monthly_cap_exceeded"
    assert detail["cap_usd"] == pytest.approx(0.01, abs=1e-9)
    # spent_usd reports the actual ledger total — the add-in renders
    # this as "you've used $X of $Y".
    assert detail["spent_usd"] == pytest.approx(0.05, abs=1e-9)


async def test_usage_endpoint_not_blocked_by_cap(
    auth_client: tuple[AsyncClient, dict[str, str], dict[str, object]],
    db_engine: AsyncEngine,
) -> None:
    """/v1/usage must remain reachable even when the user is over cap.

    Reading your own spend should never be rate-limited or capped —
    that's how the add-in renders the "limit reached" UX.
    """
    client, headers, user_info = auth_client
    user_id = UUID(user_info["id"])  # type: ignore[arg-type]

    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(monthly_usd_cap=Decimal("0.01"))
        )
        session.add(_insert_log(user_id, Decimal("9.999999")))
        await session.commit()

    resp = await client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["spent_usd"] == pytest.approx(9.999999, abs=1e-9)


# ---- Sanity: unused import safeguard --------------------------------------


def test_imports_still_resolve() -> None:
    """Defensive: catches accidental cyclic imports between billing and
    limits when this module is collected by pytest.
    """
    from gateway.billing import CapResult  # noqa: F401
    from gateway.limits import enforce_monthly_cap, enforce_rate_limit  # noqa: F401
    # ``UTC``/``timedelta`` are imported up top in case future tests need
    # to reach for explicit timestamps.
    assert UTC is not None
    assert timedelta(seconds=1).total_seconds() == 1
