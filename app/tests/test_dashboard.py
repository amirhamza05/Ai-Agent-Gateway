"""Integration tests for the /dashboard/* admin endpoints.

All tests require a real Postgres + Redis (via the db_engine / db_client
fixtures from conftest.py). The dashboard cookie auth, CSRF signing, and
flash messages are exercised end-to-end.

httpx deprecated per-request ``cookies=`` kwargs; instead we build a fresh
``AsyncClient`` with the cookie pre-loaded, using the same underlying
``db_app`` ASGI transport.
"""

from __future__ import annotations

import re
import uuid as _uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ADMIN_EMAIL = "admin@test.com"
_ADMIN_PASSWORD = "correcthorsebattery"
_USER_EMAIL = "regular@test.com"
_USER_PASSWORD = "regularpwsecure!"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _truncate_dashboard_sessions(db_engine: object) -> None:
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(text("TRUNCATE TABLE dashboard_sessions CASCADE"))


@asynccontextmanager
async def _authed_client(db_app: object, session_cookie: str) -> AsyncIterator[AsyncClient]:
    """Yield an AsyncClient that carries the dashboard_session cookie."""
    transport = ASGITransport(app=db_app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"dashboard_session": session_cookie},
    ) as ac:
        yield ac


async def _get_csrf(client: AsyncClient, url: str) -> str:
    """GET a form page and extract the csrf_token hidden input value."""
    r = await client.get(url)
    assert r.status_code == 200, f"GET {url} returned {r.status_code}: {r.text}"
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
    assert m, f"No csrf_token found in {url} response:\n{r.text[:2000]}"
    return m.group(1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_session(db_client: AsyncClient, db_engine: object) -> str:
    """Register a user, promote to admin via DB, login via dashboard form.

    Returns the raw ``dashboard_session`` cookie value so tests can build
    an authed client via ``_authed_client(db_app, session_cookie)``.
    """
    await _truncate_dashboard_sessions(db_engine)

    # Register via API
    r = await db_client.post(
        "/auth/register",
        json={"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
    )
    assert r.status_code == 201, r.text

    # Promote via direct DB update
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            text("UPDATE users SET is_admin = TRUE WHERE email = :e"),
            {"e": _ADMIN_EMAIL},
        )

    # Login via dashboard form (no cookie needed, anon client is fine)
    r = await db_client.post(
        "/dashboard/login",
        data={"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"Expected 303, got {r.status_code}: {r.text}"
    cookie = r.cookies.get("dashboard_session")
    assert cookie, f"No dashboard_session cookie in response: {dict(r.cookies)}"
    return cookie


# ===========================================================================
# 1. Unauthenticated redirect
# ===========================================================================


async def test_dashboard_root_unauthenticated_redirects_to_login(
    db_client: AsyncClient,
) -> None:
    r = await db_client.get("/dashboard/", follow_redirects=False)
    assert r.status_code == 303
    assert "/dashboard/login" in r.headers["location"]


# ===========================================================================
# 2. Non-admin login rejected
# ===========================================================================


async def test_dashboard_login_with_non_admin_user_returns_403_after_auth(
    db_client: AsyncClient, db_engine: object
) -> None:
    await _truncate_dashboard_sessions(db_engine)

    r = await db_client.post(
        "/auth/register",
        json={"email": _USER_EMAIL, "password": _USER_PASSWORD},
    )
    assert r.status_code == 201

    # Attempt dashboard login — should redirect back to login with error flash
    r = await db_client.post(
        "/dashboard/login",
        data={"email": _USER_EMAIL, "password": _USER_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/dashboard/login" in r.headers["location"]
    # No session cookie should be set
    assert "dashboard_session" not in r.cookies


# ===========================================================================
# 3. Admin login succeeds, cookie is set
# ===========================================================================


async def test_dashboard_login_with_admin_succeeds_and_sets_cookie(
    admin_session: str,
) -> None:
    assert admin_session, "Expected a non-empty session cookie"


# ===========================================================================
# 4. Logout clears cookie and revokes session
# ===========================================================================


async def test_dashboard_logout_clears_cookie_and_revokes_session(
    db_app: object, admin_session: str
) -> None:
    async with _authed_client(db_app, admin_session) as ac:
        r = await ac.post(
            "/dashboard/logout",
            data={},
            follow_redirects=False,
        )
        assert r.status_code == 303

    # Using the old session cookie should now redirect to login
    async with _authed_client(db_app, admin_session) as ac2:
        r2 = await ac2.get("/dashboard/", follow_redirects=False)
        assert r2.status_code == 303
        assert "/dashboard/login" in r2.headers["location"]


# ===========================================================================
# 5. CSRF required on POST
# ===========================================================================


async def test_csrf_token_required_on_post(
    db_client: AsyncClient, db_app: object, admin_session: str, db_engine: object
) -> None:
    # Register a user to use as target
    r = await db_client.post(
        "/auth/register",
        json={"email": "captest@test.com", "password": "correcthorsebattery"},
    )
    assert r.status_code == 201
    uid = r.json()["id"]

    async with _authed_client(db_app, admin_session) as ac:
        # POST without CSRF token → 400
        r = await ac.post(
            f"/dashboard/users/{uid}/cap",
            data={"monthly_usd_cap": "5.00"},  # no csrf_token
        )
    assert r.status_code == 400
    assert r.json()["error"] == "csrf_invalid"


# ===========================================================================
# 6. Create user returns tokens_once page
# ===========================================================================


async def test_dashboard_create_user_returns_tokens_once_page(
    db_app: object, admin_session: str
) -> None:
    async with _authed_client(db_app, admin_session) as ac:
        csrf = await _get_csrf(ac, "/dashboard/users/new")

        r = await ac.post(
            "/dashboard/users",
            data={
                "email": "newuser@example.com",
                "password": "supersecurepassword123",
                "monthly_usd_cap": "20.00",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert r.status_code == 200, f"Expected 200 tokens_once, got {r.status_code}: {r.text}"
    assert "shown only once" in r.text.lower() or "only once" in r.text.lower()
    assert "newuser@example.com" in r.text


# ===========================================================================
# 7. Created user can login via API with those credentials
# ===========================================================================


async def test_created_user_can_login_via_api_with_those_tokens(
    db_client: AsyncClient, db_app: object, admin_session: str
) -> None:
    email = "apilogintest@example.com"
    password = "apisecurepassword123"

    async with _authed_client(db_app, admin_session) as ac:
        csrf = await _get_csrf(ac, "/dashboard/users/new")
        await ac.post(
            "/dashboard/users",
            data={
                "email": email,
                "password": password,
                "monthly_usd_cap": "10.00",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )

    # Login via normal API endpoint
    r = await db_client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, f"API login failed: {r.text}"
    assert "access_token" in r.json()


# ===========================================================================
# 8. Update user cap persists to DB
# ===========================================================================


async def test_dashboard_update_user_cap_persists_to_db(
    db_client: AsyncClient, db_app: object, admin_session: str, db_engine: object
) -> None:
    # Create user via API for simplicity
    r = await db_client.post(
        "/auth/register",
        json={"email": "capupdate@example.com", "password": "capupdatepassword123"},
    )
    assert r.status_code == 201

    # Get user ID from DB
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text("SELECT id FROM users WHERE email = 'capupdate@example.com'")
        )
        row = result.first()
    assert row is not None
    uid = row[0]

    async with _authed_client(db_app, admin_session) as ac:
        csrf = await _get_csrf(ac, f"/dashboard/users/{uid}")

        r2 = await ac.post(
            f"/dashboard/users/{uid}/cap",
            data={"monthly_usd_cap": "42.00", "csrf_token": csrf},
            follow_redirects=False,
        )
    assert r2.status_code == 303

    # Verify in DB
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text("SELECT monthly_usd_cap FROM users WHERE id = :uid"),
            {"uid": uid},
        )
        db_cap = result.scalar_one()
    assert float(db_cap) == pytest.approx(42.00)


# ===========================================================================
# 9. Create model pricing invalidates cache
# ===========================================================================


async def test_dashboard_create_model_pricing_invalidates_cache_and_visible_to_v1(
    db_app: object, admin_session: str
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    model_id = f"test/new-model-{_uuid.uuid4().hex[:8]}"

    async with _authed_client(db_app, admin_session) as ac:
        csrf = await _get_csrf(ac, "/dashboard/models/new")

        r = await ac.post(
            "/dashboard/models",
            data={
                "model": model_id,
                "endpoint_kind": "messages",
                "input_per_mtoken": "5.0000",
                "output_per_mtoken": "25.0000",
                "is_allowed": "on",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    # Pricing cache should have been invalidated
    cache = db_app.state.pricing_cache  # type: ignore[attr-defined]
    assert cache._snapshot is None or cache._loaded_at == 0.0

    # Re-fetching should load from DB and include the new model
    session_factory = async_sessionmaker(
        bind=db_app.state.db_engine,  # type: ignore[attr-defined]
        expire_on_commit=False,
    )
    async with session_factory() as sess:
        prices = await cache.get_all(sess)
    assert model_id in prices


# ===========================================================================
# 10. Disabled model rejected at /v1/messages
# ===========================================================================


async def test_dashboard_disabled_model_rejected_at_v1_messages(
    db_client: AsyncClient, db_app: object, admin_session: str, db_engine: object
) -> None:
    model_id = "anthropic/claude-haiku-4.5"

    # Create a regular user for API calls
    r = await db_client.post(
        "/auth/register",
        json={"email": "v1test@example.com", "password": "v1testpassword123"},
    )
    assert r.status_code == 201

    r2 = await db_client.post(
        "/auth/login",
        json={"email": "v1test@example.com", "password": "v1testpassword123"},
    )
    assert r2.status_code == 200
    bearer = r2.json()["access_token"]
    headers = {"Authorization": f"Bearer {bearer}"}

    async with _authed_client(db_app, admin_session) as ac:
        # Get CSRF from models list
        csrf_list = await _get_csrf(ac, "/dashboard/models")

        r3 = await ac.post(
            f"/dashboard/models/{model_id}/delete",
            data={"csrf_token": csrf_list},
            follow_redirects=False,
        )
    assert r3.status_code == 303

    try:
        # Force cache reload by invalidating and re-fetching
        cache = db_app.state.pricing_cache  # type: ignore[attr-defined]
        cache.invalidate()

        # Attempt a /v1/messages call with the disabled model
        r4 = await db_client.post(
            "/v1/messages",
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=headers,
        )
        # Should be rejected — model is soft-deleted from pricing
        assert r4.status_code in (400, 402, 429), (
            f"Expected rejection, got {r4.status_code}: {r4.text}"
        )
    finally:
        # Re-enable the model so subsequent tests aren't affected
        async with db_engine.begin() as conn:  # type: ignore[attr-defined]
            await conn.execute(
                text(
                    "UPDATE model_pricing SET disabled_at = NULL, updated_at = now() "
                    "WHERE model = :m"
                ),
                {"m": model_id},
            )
        cache = db_app.state.pricing_cache  # type: ignore[attr-defined]
        cache.invalidate()


# ===========================================================================
# 11. Logs list paginates
# ===========================================================================


async def test_dashboard_logs_list_paginates(
    db_app: object, admin_session: str, db_engine: object
) -> None:
    # Insert 5 request_log rows directly
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        uid_result = await conn.execute(text("SELECT id FROM users LIMIT 1"))
        uid_row = uid_result.first()
        uid = str(uid_row[0]) if uid_row else None

        for _ in range(5):
            await conn.execute(
                text(
                    """
                    INSERT INTO request_log
                      (request_id, user_id, endpoint, status_code, created_at)
                    VALUES
                      (:rid, :uid, '/v1/messages', 200, now())
                    """
                ),
                {"rid": str(_uuid.uuid4()), "uid": uid},
            )

    async with _authed_client(db_app, admin_session) as ac:
        r1 = await ac.get("/dashboard/logs?page=1&size=2")
        assert r1.status_code == 200

        r2 = await ac.get("/dashboard/logs?page=2&size=2")
        assert r2.status_code == 200

    # Different pages should render different content
    assert r1.text != r2.text


# ===========================================================================
# 12. Logs filter by endpoint works
# ===========================================================================


async def test_dashboard_logs_filter_by_endpoint_works(
    db_app: object, admin_session: str, db_engine: object
) -> None:
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        uid_result = await conn.execute(text("SELECT id FROM users LIMIT 1"))
        uid_row = uid_result.first()
        uid = str(uid_row[0]) if uid_row else None

        await conn.execute(
            text(
                """
                INSERT INTO request_log
                  (request_id, user_id, endpoint, status_code, created_at)
                VALUES (:rid, :uid, '/v1/embeddings', 200, now())
                """
            ),
            {"rid": str(_uuid.uuid4()), "uid": uid},
        )

    async with _authed_client(db_app, admin_session) as ac:
        r = await ac.get("/dashboard/logs?endpoint=/v1/embeddings")
    assert r.status_code == 200
    assert "/v1/embeddings" in r.text


# ===========================================================================
# 13. Cost JSON returns array
# ===========================================================================


async def test_dashboard_reports_cost_json_returns_array(
    db_app: object, admin_session: str
) -> None:
    async with _authed_client(db_app, admin_session) as ac:
        r = await ac.get("/dashboard/reports/cost.json")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    for item in data:
        assert "day" in item
        assert "cost_usd" in item


# ===========================================================================
# 14. Promote CLI sets is_admin
# ===========================================================================


async def test_promote_cli_sets_is_admin(
    db_client: AsyncClient, db_engine: object
) -> None:
    """Register a user, call the promote logic directly, verify is_admin=True."""
    import os

    email = "topromote@example.com"
    r = await db_client.post(
        "/auth/register",
        json={"email": email, "password": "promotetestpwd123"},
    )
    assert r.status_code == 201

    # Verify starts as non-admin
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text("SELECT is_admin FROM users WHERE email = :e"), {"e": email}
        )
        assert result.scalar_one() is False

    # Point CLI at the test DB
    test_db = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://gateway:g6fOKG2zYvGvhJlHtMDON_g-pfu_Sh5H27q8DTJhXpc@postgres:5432/gateway_test",
    )
    original_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_db
    try:
        from gateway.cli import _promote
        rc = await _promote(email)
    finally:
        if original_url is not None:
            os.environ["DATABASE_URL"] = original_url
        else:
            os.environ.pop("DATABASE_URL", None)

    assert rc == 0

    # Verify in DB
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text("SELECT is_admin FROM users WHERE email = :e"), {"e": email}
        )
        assert result.scalar_one() is True
