"""pd dashboard

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-09 12:00:00.000000

Phase D — Admin Dashboard. Adds:

* ``users.is_admin`` boolean (default FALSE).
* ``dashboard_sessions`` table — cookie-session backing store. SHA-256 hash
  of the raw cookie token, plus expiry / revocation / activity columns.
* ``model_pricing`` table — moves the in-process pricing dictionaries from
  ``gateway.billing`` into the database so operators can edit pricing and
  the model allow-list without a deploy. The table is created **empty**;
  operators add rows from /dashboard/models. Until rows exist,
  ``billing.PricingCache`` falls back to the legacy in-process constants
  in ``gateway.billing`` so /v1 traffic keeps working.

Downgrade drops everything Phase D added but keeps ``users`` and the
existing tables untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add the is_admin flag to users. Default FALSE so all existing
    #    rows remain non-admin until promoted via the CLI.
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )

    # 2. dashboard_sessions table.
    op.create_table(
        "dashboard_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_dashboard_sessions_user_id",
        ),
        sa.UniqueConstraint(
            "session_hash",
            name="uq_dashboard_sessions_session_hash",
        ),
    )
    op.create_index(
        "ix_dashboard_sessions_user_id",
        "dashboard_sessions",
        ["user_id"],
    )
    op.create_index(
        "ix_dashboard_sessions_expires_at",
        "dashboard_sessions",
        ["expires_at"],
    )

    # 3. model_pricing table. Primary key is the model id (a TEXT slug).
    op.create_table(
        "model_pricing",
        sa.Column("model", sa.Text(), primary_key=True, nullable=False),
        sa.Column("endpoint_kind", sa.Text(), nullable=False),
        sa.Column("input_per_mtoken", sa.Numeric(10, 4), nullable=False),
        sa.Column("output_per_mtoken", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "is_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("model_pricing")
    op.drop_index(
        "ix_dashboard_sessions_expires_at",
        table_name="dashboard_sessions",
    )
    op.drop_index(
        "ix_dashboard_sessions_user_id",
        table_name="dashboard_sessions",
    )
    op.drop_table("dashboard_sessions")
    op.drop_column("users", "is_admin")
