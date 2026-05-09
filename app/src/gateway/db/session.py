"""Async engine + session factory.

The lifespan in :mod:`gateway.main` builds an engine once at startup and
disposes of it on shutdown. Per-request sessions are obtained via the
``async_sessionmaker`` returned from :func:`create_session_factory`.

We intentionally do NOT expose a module-level engine. Tests need to swap it
out for a per-DSN engine, and async resources can't safely live at module
scope when the event loop changes between tests.
"""

from __future__ import annotations

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
    return create_async_engine(
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


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an :class:`async_sessionmaker` bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
