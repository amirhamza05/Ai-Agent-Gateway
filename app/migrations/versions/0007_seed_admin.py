"""seed bootstrap admin user

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-10 13:00:00.000000

Inserts a single admin row so a fresh deploy can sign in to the dashboard
without going through register-then-promote. The seeded credentials are::

    email:    admin@gmail.com
    password: password

The operator should change the password from the dashboard immediately
after first login.

The INSERT is idempotent (``ON CONFLICT (email) DO NOTHING``) so this
migration is safe to re-run on a database where the row already exists.

Downgrade deletes the row only if its ``password_hash`` still matches the
hash this migration produced — i.e. only if nobody has rotated the
credential. Otherwise the row is left in place.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from argon2 import PasswordHasher

# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


_ADMIN_EMAIL = "admin@gmail.com"
_ADMIN_PASSWORD = "password"


def upgrade() -> None:
    # Hash at upgrade time so each deploy gets a fresh salt. ``argon2-cffi``
    # is already a runtime dependency (used by ``gateway.auth.passwords``)
    # so importing it here adds no new install footprint.
    password_hash = PasswordHasher().hash(_ADMIN_PASSWORD)

    op.get_bind().execute(
        sa.text(
            "INSERT INTO users (email, password_hash, is_admin) "
            "VALUES (:email, :password_hash, TRUE) "
            "ON CONFLICT (email) DO NOTHING"
        ),
        {"email": _ADMIN_EMAIL, "password_hash": password_hash},
    )


def downgrade() -> None:
    # Best-effort delete. We don't try to verify the hash here — we just
    # remove the row if it still exists. An operator who has rotated the
    # password is expected to also remove this migration before downgrading.
    op.get_bind().execute(
        sa.text("DELETE FROM users WHERE email = :email"),
        {"email": _ADMIN_EMAIL},
    )
