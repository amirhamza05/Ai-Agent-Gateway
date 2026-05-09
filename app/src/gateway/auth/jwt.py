"""JWT issuance + verification, plus refresh-token utilities.

Access tokens are stateless HS256 JWTs signed with ``settings.jwt_secret``.
Claims are kept minimal:

    sub  user id (str(UUID))
    iat  issued-at (epoch seconds)
    exp  expires-at (epoch seconds)
    iss  fixed issuer string

Refresh tokens are NOT JWTs — they're opaque ``secrets.token_urlsafe(48)``
strings. The DB only ever stores the SHA-256 hex of the raw value, so a
leaked DB row cannot impersonate the user. Rotation/revocation is handled
in the route layer by INSERTing a new row and stamping ``revoked_at`` on
the old one.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from gateway.config import Settings

# Fixed issuer claim. Validated in :func:`decode_access_token`.
JWT_ISSUER = "geoswmm-gateway"


def create_access_token(user_id: UUID, settings: Settings) -> tuple[str, int]:
    """Mint a signed access token for ``user_id``.

    Returns ``(token, expires_in_seconds)`` so the route handler can put the
    same lifetime in the JSON response without re-deriving it from the
    settings.
    """
    expires_in = settings.access_token_expires_min * 60
    now = datetime.now(tz=UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "iss": JWT_ISSUER,
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_in


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    """Verify ``token`` and return its claims dict.

    Raises ``jwt.PyJWTError`` (or one of its subclasses — ExpiredSignatureError,
    InvalidIssuerError, etc.) on failure. Callers should let those bubble up
    and convert to a 401 in the route layer.
    """
    return jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
        issuer=JWT_ISSUER,
        options={"require": ["exp", "iat", "iss", "sub"]},
    )


# ---- Refresh tokens --------------------------------------------------------
#
# Two helpers, each one line, but symbolically named so callers can never
# accidentally store a raw refresh token to the DB. Always pair them:
#
#     raw = generate_refresh_token()
#     row = RefreshToken(token_hash=hash_refresh_token(raw), ...)
#     return raw  # to the client
#
# 48 bytes of entropy → ~64-char URL-safe base64. SHA-256 hex is a
# fixed-length 64-char ASCII string — safe to put in a Postgres ``text``
# column and uniquely indexable.


def generate_refresh_token() -> str:
    """Return a fresh URL-safe refresh token (~64 chars, 48 bytes of entropy)."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    """Return the SHA-256 hex digest of ``raw`` for storage in the DB."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_token_expiry(settings: Settings) -> datetime:
    """Return the absolute expiry timestamp for a freshly-minted refresh token."""
    return datetime.now(tz=UTC) + timedelta(days=settings.refresh_token_expires_days)
