"""Async engine + session factory.

The lifespan in :mod:`gateway.main` builds an engine once at startup and
disposes of it on shutdown. Per-request sessions are obtained via the
``async_sessionmaker`` returned from :func:`create_session_factory`.

We intentionally do NOT expose a module-level engine. Tests need to swap it
out for a per-DSN engine, and async resources can't safely live at module
scope when the event loop changes between tests.

pgvector codec
--------------
``pgvector.asyncpg.register_vector`` must be called on each raw asyncpg
connection before any vector column is read or written. SQLAlchemy's
asyncpg dialect wraps the raw connection in an ``AdaptedConnection`` and
does NOT forward the asyncpg ``init=`` kwarg, so the canonical integration
point is the pool ``"connect"`` event on the sync façade — the adapter
exposes ``run_async`` to drive a coroutine on the dialect's event loop.
See https://github.com/pgvector/pgvector-python#sqlalchemy.
"""

from __future__ import annotations

from pgvector.asyncpg import register_vector
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an :class:`AsyncEngine` for ``database_url``.

    Args:
        database_url: A ``postgresql+asyncpg://...`` DSN.
        echo: When ``True``, log every SQL statement. Dev only.

    Returns:
        A configured :class:`AsyncEngine`. Caller owns disposal — call
        ``await engine.dispose()`` on shutdown.
    """
    engine = create_async_engine(
        database_url,
        echo=echo,
        # Conservative defaults for a small VPS. Postgres is configured for
        # max_connections=50, and we run 2 uvicorn workers, so 10 connections
        # per worker leaves headroom for migrations and one-off psql sessions.
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _register_pgvector_codec(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        # ``dbapi_conn`` is SQLAlchemy's AdaptedConnection wrapping the raw
        # asyncpg.Connection. ``run_async`` runs the coroutine on the
        # dialect's event loop synchronously from this sync callback.
        dbapi_conn.run_async(register_vector)

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an :class:`async_sessionmaker` bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


# Re-export so callers can ``from gateway.db.session import AsyncSession``
# without importing SQLAlchemy directly.
__all__ = [
    "create_engine",
    "create_session_factory",
    "AsyncSession",
    "async_sessionmaker",
]
