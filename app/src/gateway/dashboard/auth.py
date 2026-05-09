"""Dashboard cookie-session auth.

Sessions are opaque 32-byte tokens (``secrets.token_urlsafe(32)``)
hashed with SHA-256 before insert into ``dashboard_sessions``. The raw
token only exists in the response cookie and on the client. A leaked
DB row cannot impersonate the admin.

Cookie semantics:

* ``HttpOnly=True`` — JS can't read it.
* ``SameSite=lax`` — works for top-level navigation, blocks the most
  common CSRF vectors (we additionally require a per-form CSRF token,
  see :mod:`gateway.dashboard.csrf`).
* ``Secure`` — set when the request was served over HTTPS.
* ``max_age=28800`` — 8 hours, refreshed on every request via
  ``last_seen_at``.
* ``path=/dashboard`` — never sent to ``/v1/*`` or ``/auth/*``.

The ``require_admin`` dependency is the gate on every dashboard
endpoint except ``/dashboard/login``, ``/dashboard/logout``, and the
static asset mount.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db.models import DashboardSession, User

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = structlog.get_logger(__name__)

# Cookie name + lifetime constants. The path is restricted to /dashboard
# so the cookie is never sent to /v1/* or /auth/*.
SESSION_COOKIE_NAME = "dashboard_session"
SESSION_TTL = timedelta(hours=8)
SESSION_MAX_AGE_SECONDS = int(SESSION_TTL.total_seconds())
COOKIE_PATH = "/dashboard"


def _hash_session_token(raw: str) -> str:
    """Return SHA-256 hex digest of the raw cookie value."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def create_session(
    session_db: AsyncSession,
    *,
    user_id,
    request: Request,
) -> tuple[str, datetime]:
    """Mint a fresh dashboard session and persist it.

    Returns ``(raw_token, expires_at)``. The caller sets the cookie on
    the response. Caller is responsible for committing the session_db.
    """
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(tz=UTC)
    expires_at = now + SESSION_TTL
    user_agent = request.headers.get("User-Agent")
    client_ip = request.client.host if request.client else None
    row = DashboardSession(
        user_id=user_id,
        session_hash=_hash_session_token(raw_token),
        expires_at=expires_at,
        last_seen_at=now,
        user_agent=user_agent[:512] if user_agent else None,
        ip=client_ip,
    )
    session_db.add(row)
    await session_db.flush()
    return raw_token, expires_at


async def verify_session(
    session_db: AsyncSession,
    *,
    raw_token: str,
) -> tuple[User, DashboardSession] | None:
    """Look up an active session and its owning user.

    Returns ``None`` for missing / revoked / expired / inactive-user
    cases. On hit, updates ``last_seen_at`` so idle sessions show
    activity in the dashboard.
    """
    if not raw_token:
        return None
    token_hash = _hash_session_token(raw_token)

    result = await session_db.execute(
        select(DashboardSession).where(
            DashboardSession.session_hash == token_hash,
            DashboardSession.revoked_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    now = datetime.now(tz=UTC)
    expires_at = row.expires_at
    # asyncpg returns timezone-aware datetimes for TIMESTAMPTZ; defensive
    # for sqlite-style stripping if a future driver swap drops the tz.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        return None

    user = await session_db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None

    # Refresh the last-seen timestamp so the operator can tell which
    # sessions are active. Best-effort; failure here shouldn't block
    # the request.
    await session_db.execute(
        update(DashboardSession)
        .where(DashboardSession.id == row.id)
        .values(last_seen_at=now)
    )

    return user, row


async def revoke_session(
    session_db: AsyncSession,
    *,
    raw_token: str,
) -> None:
    """Mark the session matching ``raw_token`` as revoked."""
    if not raw_token:
        return
    token_hash = _hash_session_token(raw_token)
    now = datetime.now(tz=UTC)
    await session_db.execute(
        update(DashboardSession)
        .where(
            DashboardSession.session_hash == token_hash,
            DashboardSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


async def revoke_all_user_sessions(
    session_db: AsyncSession,
    *,
    user_id,
) -> None:
    """Revoke every active session for a user (used by deactivate)."""
    now = datetime.now(tz=UTC)
    await session_db.execute(
        update(DashboardSession)
        .where(
            DashboardSession.user_id == user_id,
            DashboardSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


def set_session_cookie(response: Response, raw_token: str, *, request: Request) -> None:
    """Attach the dashboard session cookie to ``response``.

    ``Secure`` is set only when the request came in over HTTPS so local
    dev (HTTP) still works without a self-signed cert.
    """
    secure = request.url.scheme == "https"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=COOKIE_PATH,
    )


def clear_session_cookie(response: Response, *, request: Request) -> None:
    """Delete the dashboard session cookie."""
    secure = request.url.scheme == "https"
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=COOKIE_PATH,
        httponly=True,
        secure=secure,
        samesite="lax",
    )


# ---- Dependencies ----------------------------------------------------


def _redirect_to_login() -> HTTPException:
    """Build the 303 → /dashboard/login exception."""
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/dashboard/login"},
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="forbidden",
    )


async def require_admin(
    request: Request,
) -> tuple[User, DashboardSession]:
    """FastAPI dependency: gate a dashboard endpoint on admin auth.

    Reads the ``dashboard_session`` cookie, looks it up in
    ``dashboard_sessions``, joins to ``users`` and ensures
    ``is_admin=True``. On any failure it raises 303 → /dashboard/login.
    On signed-in-but-not-admin it raises 403 (rendered as a "forbidden"
    page by the route layer's exception handler).

    The dependency opens its own short-lived session because dashboard
    routes mostly need their own AsyncSession parameter too — opening
    two for one request is fine and keeps the dep wiring simple.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise _redirect_to_login()

    session_factory = request.app.state.db_session_factory
    async with session_factory() as session_db:
        result = await verify_session(session_db, raw_token=raw_token)
        if result is None:
            await session_db.rollback()
            raise _redirect_to_login()
        user, row = result
        if not user.is_admin:
            await session_db.commit()
            raise _forbidden()
        # Persist the last_seen_at update.
        await session_db.commit()

    request.state.dashboard_user = user
    request.state.dashboard_session = row
    return user, row
