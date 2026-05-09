"""Shared pytest fixtures.

Tests run with ``asyncio_mode = "auto"`` (see pyproject.toml), so any
``async def test_*`` is automatically awaited — no ``@pytest.mark.asyncio``
needed.

Two flavours of test live here:

* **Unit-ish tests** (e.g. ``test_health``) use the ``client`` fixture which
  opens the ASGI lifespan but doesn't talk to a real DB.
* **Auth integration tests** (``test_auth``) use ``db_engine`` + ``db_app``
  + ``auth_client``, all of which require a real Postgres reachable at
  ``TEST_DATABASE_URL``. The CLAUDE.md mandates real DB for these — no
  mocks. The ``db_engine`` fixture runs Alembic ``upgrade head`` once per
  test session and TRUNCATEs ``users`` + ``refresh_tokens`` between tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

# We populate any required settings *before* importing the app so
# pydantic-settings doesn't blow up when ``.env`` isn't present in CI.
# These values are intentionally fake; tests that hit Postgres/Redis must
# override them via fixtures.
_DEFAULT_TEST_ENV = {
    "POSTGRES_PASSWORD": "test-password-not-real",
    "DATABASE_URL": "postgresql+asyncpg://gateway:test@postgres:5432/gateway_test",
    "REDIS_URL": "redis://redis:6379/1",
    "JWT_SECRET": "test-jwt-secret-do-not-use-in-prod",
    "OPENROUTER_API_KEY": "sk-or-test",
    "QDRANT_URL": "https://test.cloud.qdrant.io",
    "QDRANT_API_KEY": "test-qdrant-key",
    # Match .env.example so ``model_not_allowed`` tests behave the same
    # in CI as they do against a real .env. Tests that need a different
    # allow-list can override per-test via env vars before importing the
    # app, but the suite as a whole assumes these models are present.
    "ALLOWED_MODELS": (
        "anthropic/claude-opus-4.7,"
        "anthropic/claude-sonnet-4.6,"
        "anthropic/claude-haiku-4.5,"
        "openai/text-embedding-3-small,"
        "openai/text-embedding-3-large"
    ),
    "LOG_FORMAT": "console",
    "LOG_LEVEL": "WARNING",
}
for key, value in _DEFAULT_TEST_ENV.items():
    os.environ.setdefault(key, value)

# Auth tests target a dedicated test DB. The rest of the suite uses the
# default DATABASE_URL, which doesn't have to exist for the lifespan-only
# tests because we only construct the engine — we don't connect.
_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Yield an httpx ``AsyncClient`` wired to the FastAPI app via ASGI.

    Uses :class:`httpx.ASGITransport` so requests never touch the network —
    the app's lifespan still runs (engine/Redis are opened) so this exercises
    a realistic startup path.
    """
    # Imported lazily so the env defaults above are in place first.
    from gateway.main import create_app

    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Manually drive the lifespan so startup/shutdown hooks run.
        async with app.router.lifespan_context(app):
            yield ac


# ---- DB-backed fixtures (used by test_auth) -------------------------------


def _ensure_test_db_url() -> str:
    if not _TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set — auth integration tests skipped.")
    return _TEST_DATABASE_URL


@pytest.fixture(scope="session")
def _migrated_test_db() -> str:
    """Run Alembic ``upgrade head`` against ``TEST_DATABASE_URL`` once.

    Synchronous on purpose: Alembic's env.py drives its own ``asyncio.run``,
    and wrapping that in an async fixture creates a session-scoped loop that
    gets closed before per-test engine teardown runs (causing
    ``RuntimeError: Event loop is closed`` during pool dispose).
    """
    dsn = _ensure_test_db_url()

    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        from alembic import command
        from alembic.config import Config

        here = os.path.dirname(os.path.abspath(__file__))
        ini = os.path.join(here, "..", "alembic.ini")

        cfg = Config(ini)
        cfg.set_main_option(
            "script_location",
            os.path.normpath(os.path.join(here, "..", "migrations")),
        )
        command.upgrade(cfg, "head")
        return dsn
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


@pytest.fixture
async def db_engine(_migrated_test_db: str) -> AsyncIterator[object]:
    """Per-test engine pointed at the test DB.

    Truncates ``request_log``, ``refresh_tokens``, and ``users`` after
    each test so tests don't see each other's rows. Uses TRUNCATE ...
    CASCADE so FK order doesn't matter — but we still list ``request_log``
    first because it FKs to ``users`` and a non-cascading truncate would
    fail otherwise.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_migrated_test_db, pool_pre_ping=True)
    try:
        yield engine
    finally:
        # Truncate first, THEN dispose, so the next test starts clean.
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE request_log, refresh_tokens, users "
                    "RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


@pytest.fixture
async def db_app(db_engine: object) -> AsyncIterator[object]:
    """A fresh FastAPI app whose state.db_session_factory points at the test DB.

    The lifespan would otherwise build an engine off DATABASE_URL (the
    "real" one). We swap it for the test engine after lifespan startup.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from gateway.main import create_app

    app = create_app()
    yield_ctx = app.router.lifespan_context(app)
    await yield_ctx.__aenter__()
    try:
        # Replace the lifespan-built engine + factory with one bound to the
        # test DB. The lifespan engine is disposed when we exit.
        app.state.db_engine = db_engine
        app.state.db_session_factory = async_sessionmaker(
            bind=db_engine,
            expire_on_commit=False,
        )
        yield app
    finally:
        await yield_ctx.__aexit__(None, None, None)


@pytest.fixture
async def db_client(db_app: object) -> AsyncIterator[AsyncClient]:
    """An ``AsyncClient`` wired to the DB-backed app."""
    transport = ASGITransport(app=db_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---- P3 fixtures: streaming + auth helpers --------------------------------


_AUTH_TEST_EMAIL = "stream-test@example.com"
_AUTH_TEST_PASSWORD = "correcthorsebattery"


@pytest.fixture
async def auth_client(
    db_client: AsyncClient,
) -> AsyncIterator[tuple[AsyncClient, dict[str, str], dict[str, object]]]:
    """Register + log in a fresh user, return ``(client, headers, user_info)``.

    Centralises the register-then-login dance so streaming tests stay
    focused on the streaming behaviour rather than auth boilerplate.
    The returned ``user_info`` carries the ``id`` from the register
    response so tests can assert on ``request_log.user_id`` without
    re-querying.
    """
    reg = await db_client.post(
        "/auth/register",
        json={"email": _AUTH_TEST_EMAIL, "password": _AUTH_TEST_PASSWORD},
    )
    assert reg.status_code == 201, reg.text
    user_info = reg.json()

    login = await db_client.post(
        "/auth/login",
        json={"email": _AUTH_TEST_EMAIL, "password": _AUTH_TEST_PASSWORD},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    yield db_client, headers, user_info


@pytest.fixture
def openrouter_mock():  # type: ignore[no-untyped-def]
    """Yield a ``respx.MockRouter`` patched onto httpx for all clients.

    ``assert_all_called=False`` because some negative tests don't actually
    reach the upstream (e.g. 400 for unknown model), and we don't want
    those to fail with "unused mock route". Tests that DO want to assert
    a call happened can check ``route.called`` directly.
    """
    import respx

    with respx.mock(
        base_url="https://openrouter.ai/api/v1",
        assert_all_called=False,
    ) as router:
        yield router


@pytest.fixture
def qdrant_mock():  # type: ignore[no-untyped-def]
    """Yield a ``respx.MockRouter`` patched onto the gateway's Qdrant URL.

    Mirrors :func:`openrouter_mock` but pointed at ``QDRANT_URL`` so the
    Qdrant routes' upstream client gets intercepted. The default value
    in the test env is ``https://test.cloud.qdrant.io``; tests that set a
    different ``QDRANT_URL`` would need to update this fixture too.

    ``assert_all_called=False`` for the same reason as the OpenRouter
    fixture: negative tests (e.g. 400 from collection-name validation)
    don't actually reach upstream.
    """
    import os

    import respx

    base_url = os.environ.get("QDRANT_URL", "https://test.cloud.qdrant.io")
    with respx.mock(base_url=base_url, assert_all_called=False) as router:
        yield router


# ---- P4 fixtures: Redis-backed safety nets --------------------------------


@pytest.fixture
async def redis_client() -> AsyncIterator[object]:
    """Per-test async Redis client pointed at REDIS_URL.

    Skips the test if Redis isn't reachable — same pattern the DB
    fixtures use. Flushes the test DB before AND after so unrelated
    keys can't bleed in (e.g. a previous test that crashed mid-write).

    We deliberately use a dedicated DB number (1 by default) so the
    test suite never collides with a developer's local app stack.
    """
    import os

    import redis.asyncio as redis_asyncio

    url = os.environ.get("REDIS_URL", "redis://redis:6379/1")
    client = redis_asyncio.from_url(
        url,
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception:  # pragma: no cover - skip when Redis unreachable
        await client.aclose()
        pytest.skip(f"Redis at {url} not reachable — rate-limit tests skipped.")

    await client.flushdb()
    try:
        yield client
    finally:
        try:
            await client.flushdb()
        finally:
            await client.aclose()
