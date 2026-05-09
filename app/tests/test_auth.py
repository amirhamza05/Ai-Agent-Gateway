"""Integration tests for the /auth/* and protected /v1/usage endpoints.

These tests hit a real Postgres at ``TEST_DATABASE_URL`` (per CLAUDE.md);
no DB mocks. The ``db_client`` fixture in ``conftest.py`` truncates
``users`` and ``refresh_tokens`` between tests so each case starts fresh.

Pass count: **13 tests** when the test DB is reachable. If
``TEST_DATABASE_URL`` is unset, every test in this module is skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt as pyjwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from gateway.config import get_settings
from gateway.db.models import RefreshToken

# 12-char minimum password — anything shorter must be rejected at validation.
_VALID_PASSWORD = "correcthorsebattery"
_DEFAULT_EMAIL = "alice@example.com"


async def _register(client: AsyncClient, email: str = _DEFAULT_EMAIL,
                    password: str = _VALID_PASSWORD) -> dict[str, object]:
    resp = await client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(client: AsyncClient, email: str = _DEFAULT_EMAIL,
                 password: str = _VALID_PASSWORD) -> dict[str, object]:
    resp = await client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- /auth/register --------------------------------------------------------


async def test_register_creates_user_and_returns_201(db_client: AsyncClient) -> None:
    body = await _register(db_client)
    assert isinstance(body["id"], str)
    assert body["email"] == _DEFAULT_EMAIL


async def test_register_duplicate_email_returns_409(db_client: AsyncClient) -> None:
    await _register(db_client)
    resp = await db_client.post(
        "/auth/register",
        json={"email": _DEFAULT_EMAIL, "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "email_taken"}


async def test_register_short_password_returns_422(db_client: AsyncClient) -> None:
    resp = await db_client.post(
        "/auth/register",
        json={"email": "shortpw@example.com", "password": "short"},
    )
    # FastAPI's default validation 422; we don't customise the shape.
    assert resp.status_code == 422


# ---- /auth/login -----------------------------------------------------------


async def test_login_with_valid_credentials_returns_tokens(db_client: AsyncClient) -> None:
    await _register(db_client)
    body = await _login(db_client)
    assert body["token_type"] == "Bearer"
    assert isinstance(body["access_token"], str)
    assert isinstance(body["refresh_token"], str)
    assert body["expires_in"] > 0


async def test_login_with_wrong_password_returns_401(db_client: AsyncClient) -> None:
    await _register(db_client)
    resp = await db_client.post(
        "/auth/login",
        json={"email": _DEFAULT_EMAIL, "password": "wrong-password-of-len"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "invalid_credentials"}


async def test_login_with_unknown_email_returns_401_same_shape(db_client: AsyncClient) -> None:
    """Unknown-email response must be byte-for-byte identical to wrong-password.

    No user enumeration via differential responses.
    """
    # No registration first.
    resp = await db_client.post(
        "/auth/login",
        json={"email": "ghost@example.com", "password": _VALID_PASSWORD},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "invalid_credentials"}


# ---- Protected /v1/usage ---------------------------------------------------


async def test_protected_endpoint_requires_bearer(db_client: AsyncClient) -> None:
    resp = await db_client.get("/v1/usage")
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "unauthorized"}


async def test_protected_endpoint_with_valid_token_returns_200(db_client: AsyncClient) -> None:
    reg = await _register(db_client)
    tokens = await _login(db_client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    resp = await db_client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == reg["id"]
    # Placeholder values until P3 lands request_log.
    assert body["spent_usd"] == 0.0
    assert body["request_count"] == 0
    assert isinstance(body["monthly_usd_cap"], float)


# ---- /auth/refresh ---------------------------------------------------------


async def test_refresh_rotates_token_and_revokes_old(db_client: AsyncClient) -> None:
    await _register(db_client)
    tokens = await _login(db_client)

    resp = await db_client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert resp.status_code == 200, resp.text
    new_tokens = resp.json()
    # New refresh token must be different from the old one.
    assert new_tokens["refresh_token"] != tokens["refresh_token"]
    assert new_tokens["access_token"]

    # Old refresh token is now revoked — re-presenting it should 401.
    # Note: presenting a *revoked* token also triggers reuse-detection,
    # which revokes ALL of the user's other active tokens. That's tested
    # separately below; here we just assert the 401.
    replay = await db_client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert replay.status_code == 401
    assert replay.json()["detail"] == {"error": "invalid_refresh_token"}


async def test_refresh_with_revoked_token_revokes_all_user_tokens(
    db_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Reuse detection: presenting a revoked refresh token must lock the user out.

    All of that user's currently-active refresh tokens get ``revoked_at``
    stamped, so the legitimate client (still holding a fresh, valid token)
    is forced back to /auth/login. This is the stolen-token defence.
    """
    await _register(db_client)
    tokens = await _login(db_client)

    # First rotation: valid → success, old token now revoked.
    rotated = await db_client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert rotated.status_code == 200
    fresh_refresh = rotated.json()["refresh_token"]

    # Replay the revoked token. Must 401 AND revoke fresh_refresh too.
    replay = await db_client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert replay.status_code == 401

    # Now the previously-valid fresh_refresh is also revoked.
    aftermath = await db_client.post(
        "/auth/refresh",
        json={"refresh_token": fresh_refresh},
    )
    assert aftermath.status_code == 401

    # Defensive sanity check via direct DB query: every refresh_tokens
    # row for this user has revoked_at set.
    SessionLocal = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        rows = (await session.execute(select(RefreshToken))).scalars().all()
        assert rows, "expected refresh_tokens rows to exist"
        assert all(r.revoked_at is not None for r in rows)


# ---- /auth/logout ----------------------------------------------------------


async def test_logout_revokes_refresh_token(db_client: AsyncClient) -> None:
    await _register(db_client)
    tokens = await _login(db_client)

    resp = await db_client.post(
        "/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert resp.status_code == 204

    # Subsequent refresh attempts must fail (token now revoked).
    replay = await db_client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert replay.status_code == 401


async def test_logout_unknown_token_still_returns_204(db_client: AsyncClient) -> None:
    """Logout never reveals whether the token was valid."""
    resp = await db_client.post(
        "/auth/logout",
        json={"refresh_token": "definitely-not-a-real-token"},
    )
    assert resp.status_code == 204


# ---- Expired JWT -----------------------------------------------------------


async def test_expired_jwt_returns_401(db_client: AsyncClient) -> None:
    """Manually mint a JWT with exp in the past and confirm /v1/usage rejects it."""
    settings = get_settings()
    now = datetime.now(tz=UTC)
    expired_payload = {
        "sub": str(uuid4()),
        "iat": int((now - timedelta(hours=2)).timestamp()),
        "exp": int((now - timedelta(hours=1)).timestamp()),
        "iss": "geoswmm-gateway",
    }
    expired_token = pyjwt.encode(
        expired_payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    resp = await db_client.get(
        "/v1/usage",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "unauthorized"}
