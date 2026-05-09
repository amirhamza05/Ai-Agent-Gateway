"""Per-session CSRF tokens for dashboard forms.

Tokens are signed payloads of ``(user_id, session_id)`` produced by
:class:`itsdangerous.URLSafeTimedSerializer`. The cookie session
already binds the browser identity; the CSRF token additionally binds
the form submission to that specific session, so a malicious page
that tricks the admin's browser into POSTing can't pre-mint a valid
token.

The literal field name is ``csrf_token`` — referenced by both the
templates (hidden input) and the route handlers (form field name).
"""

from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Stable salt — signed payloads from a different salt won't validate.
_CSRF_SALT = "dashboard-csrf"
# 8 hours, matching the session TTL.
_CSRF_MAX_AGE_SECONDS = 8 * 60 * 60


def _serializer(secret: str) -> URLSafeTimedSerializer:
    """Build a fresh serializer.

    Cheap; itsdangerous serializers don't carry mutable state worth
    caching, and this avoids a process-wide singleton that would have
    to be reset for tests that rotate ``JWT_SECRET``.
    """
    return URLSafeTimedSerializer(secret, salt=_CSRF_SALT)


def issue_csrf(*, secret: str, user_id: Any, session_id: Any) -> str:
    """Mint a CSRF token bound to ``(user_id, session_id)``."""
    return _serializer(secret).dumps(
        {"u": str(user_id), "s": str(session_id)}
    )


def verify_csrf(
    *,
    secret: str,
    token: str,
    user_id: Any,
    session_id: Any,
    max_age: int = _CSRF_MAX_AGE_SECONDS,
) -> bool:
    """Return True iff ``token`` is a valid CSRF for ``(user_id, session_id)``."""
    if not token:
        return False
    try:
        payload = _serializer(secret).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("u") == str(user_id) and payload.get("s") == str(session_id)
