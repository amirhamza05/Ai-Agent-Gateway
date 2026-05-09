"""p3 request_log

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-09 11:00:00.000000

Phase 3 — Messages passthrough. Creates the ``request_log`` table that the
streaming ``/v1/messages`` handler writes into and that ``/v1/usage`` and
the (P4) monthly cap query read from.

Schema mirrors §7 of ``GatewayServerPlan.md``:

* BIGSERIAL ``id`` for monotonic ordering / cheap pagination.
* ``request_id`` UUID — the value echoed in the ``X-Request-Id`` header
  and used as a join key against client-side logs.
* ``user_id`` is nullable but FK-constrained, leaving room for future
  unauthenticated endpoints to log without breaking referential integrity.
* JSONB request_body + TEXT response_body — Postgres TOAST/LZ compresses
  these automatically past ~2 KB, so no app-side compression.
* INET ``client_ip`` — accepts a plain string on insert; SQLAlchemy/asyncpg
  handles the cast.

Indexes:

* ``ix_request_log_user_created`` on ``(user_id, created_at DESC)`` — drives
  the monthly-cap query and ``/v1/usage`` summaries.
* ``ix_request_log_created_at`` on ``created_at`` — drives retention
  sweeps. Single-column so we can range-scan by date directly.

The DESC ordering on the composite index is meaningful: Postgres can serve
the dominant "newest rows for this user" pattern from the index without a
sort step.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_log",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("client_version", sa.String(), nullable=True),
        sa.Column("client_ip", postgresql.INET(), nullable=True),
        sa.Column("request_body", postgresql.JSONB(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("request_bytes", sa.Integer(), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_request_log_user_id",
        ),
    )

    # Composite index uses DESC on created_at because the dominant access
    # pattern (monthly-cap aggregate, /v1/usage summary) wants the newest
    # rows for a user. Postgres can serve those reads directly off the
    # index without a sort step. ``op.create_index`` with raw SQL keeps the
    # DESC ordering — passing ``["user_id", "created_at"]`` would default
    # to ASC and silently degrade.
    op.execute(
        "CREATE INDEX ix_request_log_user_created "
        "ON request_log (user_id, created_at DESC)"
    )

    op.create_index(
        "ix_request_log_created_at",
        "request_log",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_request_log_created_at", table_name="request_log")
    op.drop_index("ix_request_log_user_created", table_name="request_log")
    op.drop_table("request_log")
