"""Argon2id password hashing.

Thin wrapper around ``argon2.PasswordHasher`` so the rest of the codebase
doesn't have to know which library we're using. Cost parameters come from
``argon2-cffi`` defaults — they're already calibrated against modern
hardware. Don't downtune without a benchmark.

A single module-level :class:`argon2.PasswordHasher` instance is reused —
constructing one is non-trivial and there's no per-request state.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# argon2-cffi defaults: time_cost=3, memory_cost=65536 (64 MiB), parallelism=4.
# Suitable for modern server CPUs; one verify is ~50ms.
_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Return an argon2id-encoded hash for ``plain``.

    The returned string includes algorithm, parameters, salt, and digest in
    PHC format, e.g. ``$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>``. Store
    it as-is in ``users.password_hash`` and pass it to :func:`verify_password`
    on login.
    """
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return ``True`` iff ``plain`` matches ``hashed``.

    ``argon2.exceptions.VerifyMismatchError`` (wrong password) and any other
    ``argon2.exceptions.VerificationError`` (malformed hash, etc.) collapse
    to ``False`` so callers can branch on a single boolean. We deliberately
    do NOT distinguish between "wrong password" and "corrupt hash" at the
    API layer — both surface as ``invalid_credentials``.
    """
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        # Any other argon2 error (malformed hash, unsupported variant) is
        # treated as a non-match. Logging is the caller's job.
        return False


def needs_rehash(hashed: str) -> bool:
    """Return ``True`` if ``hashed`` was produced with weaker parameters.

    Call after a successful :func:`verify_password` to decide whether to
    re-hash and persist with the current cost. We don't auto-upgrade here
    because that requires a DB write the caller may not be ready for.
    """
    return _hasher.check_needs_rehash(hashed)
