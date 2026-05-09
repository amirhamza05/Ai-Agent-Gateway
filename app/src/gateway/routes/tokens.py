"""API token management + PC-locked connect endpoint.

Three management endpoints (JWT-authenticated):

* ``POST /auth/tokens``        — mint a new API token; raw value returned once.
* ``GET  /auth/tokens``        — list the caller's tokens (hashes never exposed).
* ``DELETE /auth/tokens/{id}`` — revoke (soft-delete) a token.

One connect endpoint (no JWT required — this IS the credential exchange):

* ``POST /auth/token/connect``

  The ArcPy add-in sends the raw token and a stable machine identifier
  (Windows machine GUID, hashed MAC, etc.). The gateway:

  1. Hashes the token and looks it up.
  2. If ``machine_fingerprint`` is NULL → first use: locks it to this machine
     and returns a short-lived JWT.
  3. If ``machine_fingerprint`` matches → returns a fresh JWT.
  4. If ``machine_fingerprint`` differs → 403 ``machine_locked``.

The short-lived JWT is identical in shape to the one issued by ``/auth/login``
so the add-in can reuse the same ``Bearer`` header logic for all ``/v1/*``
requests.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.deps import get_db_session, require_user
from gateway.auth.jwt import create_access_token
from gateway.config import Settings, get_settings
from gateway.db.models import ApiToken, User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["tokens"])

_TOKEN_BYTES = 32  # 32 bytes = 256-bit entropy, URL-safe base64 ~43 chars


def _generate_raw_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---- Pydantic models -------------------------------------------------------


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTokenRequest(_StrictModel):
    description: str = Field(..., min_length=1, max_length=255)
    author: str = Field(..., min_length=1, max_length=255)


class CreateTokenResponse(BaseModel):
    id: str
    token: str  # raw value — shown only here, never again
    description: str
    author: str
    created_at: str


class TokenSummary(BaseModel):
    id: str
    description: str
    author: str
    machine_fingerprint: str | None
    is_active: bool
    created_at: str
    last_used_at: str | None
    expires_at: str | None


class ConnectRequest(_StrictModel):
    token: str
    machine_id: str = Field(..., min_length=1, max_length=512)


class ConnectResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str = "Bearer"


# ---- Helpers ---------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


# ---- Endpoints -------------------------------------------------------------


@router.post(
    "/tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateTokenResponse,
    summary="Mint a new API token",
)
async def create_token(
    body: CreateTokenRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_db_session),
) -> CreateTokenResponse:
    """Create an API token owned by the authenticated user.

    The raw token is returned **once**. Store it securely — it cannot be
    retrieved again. The gateway only keeps the SHA-256 hash.
    """
    raw = _generate_raw_token()
    row = ApiToken(
        user_id=user.id,
        token_hash=_hash_token(raw),
        description=body.description,
        author=body.author,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info("tokens.created", user_id=str(user.id), token_id=str(row.id))
    return CreateTokenResponse(
        id=str(row.id),
        token=raw,
        description=row.description,
        author=row.author,
        created_at=_iso(row.created_at) or "",
    )


@router.get(
    "/tokens",
    response_model=list[TokenSummary],
    summary="List my API tokens",
)
async def list_tokens(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[TokenSummary]:
    """Return all active tokens owned by the caller (hashes never exposed)."""
    result = await session.execute(
        select(ApiToken)
        .where(ApiToken.user_id == user.id, ApiToken.is_active.is_(True))
        .order_by(ApiToken.created_at.desc())
    )
    rows = result.scalars().all()
    return [
        TokenSummary(
            id=str(r.id),
            description=r.description,
            author=r.author,
            machine_fingerprint=r.machine_fingerprint,
            is_active=r.is_active,
            created_at=_iso(r.created_at) or "",
            last_used_at=_iso(r.last_used_at),
            expires_at=_iso(r.expires_at),
        )
        for r in rows
    ]


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API token",
)
async def revoke_token(
    token_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Soft-delete a token owned by the caller.

    Returns 204 whether or not the token existed, to avoid leaking which
    IDs are valid. Non-owners cannot revoke another user's token because
    the WHERE clause includes ``user_id``.
    """
    await session.execute(
        update(ApiToken)
        .where(ApiToken.id == token_id, ApiToken.user_id == user.id)
        .values(is_active=False)
    )
    await session.commit()
    logger.info("tokens.revoked", user_id=str(user.id), token_id=str(token_id))


@router.post(
    "/token/connect",
    response_model=ConnectResponse,
    summary="Exchange an API token for a short-lived JWT (PC-locked)",
)
async def token_connect(
    body: ConnectRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> ConnectResponse:
    """Validate an API token and bind it to a machine.

    * **First connect**: if the token has no machine locked yet, the
      provided ``machine_id`` is stored and a JWT is returned.
    * **Same machine**: ``machine_id`` matches stored fingerprint → JWT
      returned.
    * **Different machine**: 403 ``machine_locked`` — the token is already
      bound to another PC.

    On success ``last_used_at`` is updated and a fresh access JWT is
    returned. The caller uses this JWT as a normal ``Authorization: Bearer``
    header on subsequent ``/v1/*`` requests.
    """
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "invalid_token"},
    )

    token_hash = _hash_token(body.token)
    result = await session.execute(
        select(ApiToken).where(ApiToken.token_hash == token_hash)
    )
    row = result.scalar_one_or_none()

    if row is None or not row.is_active:
        logger.info("tokens.connect_invalid_or_inactive")
        raise invalid

    now = datetime.now(tz=UTC)

    if row.expires_at is not None and row.expires_at <= now:
        logger.info("tokens.connect_expired", token_id=str(row.id))
        raise invalid

    # PC-lock check
    if row.machine_fingerprint is None:
        # First connection — bind to this machine
        row.machine_fingerprint = body.machine_id
        logger.info(
            "tokens.connect_machine_bound",
            token_id=str(row.id),
            user_id=str(row.user_id),
        )
    elif row.machine_fingerprint != body.machine_id:
        logger.warning(
            "tokens.connect_machine_mismatch",
            token_id=str(row.id),
            user_id=str(row.user_id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "machine_locked"},
        )

    # Load the user so we can verify it's still active
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        logger.info("tokens.connect_user_inactive", token_id=str(row.id))
        raise invalid

    row.last_used_at = now
    await session.commit()

    access_token, expires_in = create_access_token(user.id, settings)
    logger.info(
        "tokens.connect_ok",
        token_id=str(row.id),
        user_id=str(user.id),
    )
    return ConnectResponse(access_token=access_token, expires_in=expires_in)
