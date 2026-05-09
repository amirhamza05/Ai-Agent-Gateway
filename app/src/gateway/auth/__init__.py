"""Authentication subsystem.

Modules:
    * ``routes`` — ``/auth/register``, ``/auth/login``, ``/auth/refresh``,
      ``/auth/logout``.
    * ``jwt`` — issue/verify HS256 access tokens; refresh-token utilities.
    * ``passwords`` — argon2id hash + verify wrappers.
    * ``deps`` — ``get_db_session`` and ``require_user`` FastAPI dependencies.
"""
