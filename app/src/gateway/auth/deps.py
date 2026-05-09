"""FastAPI auth dependencies.

Two callables are exported:

* :func:`get_db_session` — opens a single :class:`AsyncSession` per request,
  pulled from the session factory built in the app lifespan. The factory
  lives on ``request.app.state``, so we reach it through the ``Request``
  object rather than re-creating an engine per call.

* :func:`require_user` — extracts the bearer token, verifies the JWT, and
  loads the matching :class:`User` row. Any failure converts to ``401`` with
  the stable ``{"error": "unauthorized"}`` shape that the add-in keys off.

Routes that need the current user just do
``user: User = Depends(require_user)`` and let FastAPI wire the rest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import jwt
import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.jwt import decode_access_token
from gateway.config import Settings, get_settings
from gateway.db.models import User

logger = structlog.get_logger(__name__)

# auto_error=False so we control the 401 shape ourselves rather than letting
# FastAPI's default ``{"detail": "Not authenticated"}`` leak out.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` bound to the request's app engine.

    The session is closed in ``finally`` so a failure halfway through a
    handler still returns the connection to the pool. We do NOT begin a
    transaction here — handlers commit explicitly.
    """
    session_factory = request.app.state.db_session_factory
    session: AsyncSession = session_factory()
    try:
        yield session
    finally:
        await session.close()


async def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Resolve the request's bearer token to a :class:`User`.

    Raises 401 with ``{"error": "unauthorized"}`` for any failure mode:
    missing/malformed header, invalid signature, expired token, or a user
    row that's been deleted or deactivated. We log a structured event but
    never log the raw token or the password hash.
    """
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(credentials.credentials, settings)
    except jwt.PyJWTError as exc:
        # Don't echo the exception text — it leaks token internals to the
        # client. The structured log line is enough for ops.
        logger.info("auth.jwt_invalid", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    sub = claims.get("sub")
    try:
        user_id = UUID(str(sub))
    except (TypeError, ValueError) as exc:
        logger.info("auth.jwt_bad_sub")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        logger.info("auth.user_missing_or_inactive", user_id=str(user_id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user
