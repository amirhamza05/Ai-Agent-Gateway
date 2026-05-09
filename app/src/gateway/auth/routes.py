"""``/auth/*`` HTTP endpoints.

Implements the four-endpoint flow from §6 of the plan:

* ``POST /auth/register`` — create a user. 409 on duplicate email.
* ``POST /auth/login`` — exchange password for ``(access, refresh)``.
* ``POST /auth/refresh`` — single-use rotation; returns a fresh pair.
* ``POST /auth/logout`` — revoke a refresh token.

Conventions:

* Email is lowercased + stripped on every INSERT and lookup.
* Refresh tokens stored only as SHA-256 hex; raw tokens never touch the DB.
* Business errors use ``{"error": "..."}`` so the add-in can branch on a
  stable code. FastAPI's default 422 is fine for input validation.
* Login returns the same 401 for "no such user" and "wrong password" so we
  don't leak which emails exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session
from gateway.auth.jwt import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    refresh_token_expiry,
)
from gateway.auth.passwords import hash_password, verify_password
from gateway.config import Settings, get_settings
from gateway.db.models import RefreshToken, User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---- Pydantic models -------------------------------------------------------


class _StrictModel(BaseModel):
    """Base for request bodies — reject unexpected fields."""

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(_StrictModel):
    email: EmailStr
    # 12-char minimum per CLAUDE.md and the plan.
    password: str = Field(..., min_length=12)


class RegisterResponse(BaseModel):
    id: str
    email: str


class LoginRequest(_StrictModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """Returned by /auth/login AND /auth/refresh.

    /auth/refresh rotates the refresh token on every successful call, so it
    too returns both halves of the pair. There is intentionally only one
    response shape here — keep clients simple.
    """

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: Literal["Bearer"] = "Bearer"


class RefreshRequest(_StrictModel):
    refresh_token: str


class LogoutRequest(_StrictModel):
    refresh_token: str


# ---- Helpers ---------------------------------------------------------------


def _normalise_email(email: str) -> str:
    """Lowercase + strip. Apply on every INSERT and every lookup."""
    return email.lower().strip()


async def _issue_token_pair(
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> TokenResponse:
    """Mint an access JWT + a fresh refresh row, return both."""
    access_token, expires_in = create_access_token(user.id, settings)
    raw_refresh = generate_refresh_token()
    refresh_row = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=refresh_token_expiry(settings),
    )
    session.add(refresh_row)
    await session.commit()
    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=expires_in,
    )


# ---- Endpoints -------------------------------------------------------------


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Create a new user account",
)
async def register(
    body: RegisterRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> RegisterResponse:
    email = _normalise_email(body.email)
    user = User(
        email=email,
        password_hash=hash_password(body.password),
        # Settings stores the cap as a float for ergonomics; convert via
        # str() so we don't pick up a binary-float artefact (10.00 → 10.0
        # is fine, but anything finer would round wrong).
        monthly_usd_cap=Decimal(str(settings.default_monthly_usd_cap)),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Duplicate email — surface the stable error code the add-in checks.
        logger.info("auth.register_duplicate_email")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "email_taken"},
        ) from None

    await session.refresh(user)
    logger.info("auth.register_ok", user_id=str(user.id))
    return RegisterResponse(id=str(user.id), email=user.email)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange email + password for access and refresh tokens",
)
async def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    email = _normalise_email(body.email)
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Same response for "no such user" and "wrong password" to avoid
    # account enumeration. Verify against a real-or-dummy hash so timing
    # is uniform regardless of which branch we're on.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "invalid_credentials"},
    )

    if user is None:
        # Burn ~argon2-verify time so an attacker can't distinguish unknown
        # email from wrong password by latency.
        verify_password(body.password, hash_password("dummy-password-not-real"))
        logger.info("auth.login_unknown_email")
        raise invalid

    if not verify_password(body.password, user.password_hash):
        logger.info("auth.login_bad_password", user_id=str(user.id))
        raise invalid

    if not user.is_active:
        logger.info("auth.login_inactive_user", user_id=str(user.id))
        raise invalid

    tokens = await _issue_token_pair(session, user, settings)
    logger.info("auth.login_ok", user_id=str(user.id))
    return tokens


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the refresh token and mint a new access token",
)
async def refresh(
    body: RefreshRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "invalid_refresh_token"},
    )

    token_hash = hash_refresh_token(body.refresh_token)

    # Look up by hash regardless of revocation state so we can detect reuse
    # of a previously-revoked token (stolen-token replay scenario).
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    row = result.scalar_one_or_none()

    if row is None:
        logger.info("auth.refresh_unknown_token")
        raise invalid

    now = datetime.now(tz=UTC)

    # Reuse detection: a token we've previously revoked is being replayed.
    # Treat as a compromise and revoke ALL of that user's active refresh
    # tokens. The legitimate client will be forced to log in again, but
    # the attacker is locked out too.
    if row.revoked_at is not None:
        logger.warning("auth.refresh_reuse_detected", user_id=str(row.user_id))
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == row.user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()
        raise invalid

    if row.expires_at <= now:
        logger.info("auth.refresh_expired_token", user_id=str(row.user_id))
        raise invalid

    # Single-use rotation: revoke the consumed row and mint a fresh pair.
    row.revoked_at = now
    await session.flush()

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        # User was deleted or deactivated between issuing and refreshing.
        await session.commit()
        logger.info("auth.refresh_user_inactive", user_id=str(row.user_id))
        raise invalid

    tokens = await _issue_token_pair(session, user, settings)
    logger.info("auth.refresh_ok", user_id=str(user.id))
    return tokens


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a refresh token",
)
async def logout(
    body: LogoutRequest,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Mark the matching refresh-token row as revoked.

    Returns 204 whether or not a row was found, to avoid leaking which
    tokens exist. (An attacker who already has the raw token can confirm
    its validity via /auth/refresh anyway, but logout shouldn't be a side
    channel.)
    """
    token_hash = hash_refresh_token(body.refresh_token)
    now = datetime.now(tz=UTC)
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.commit()
    logger.info("auth.logout_ok")
