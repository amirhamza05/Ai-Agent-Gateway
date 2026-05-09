"""api tokens and chat_id

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-09 14:00:00.000000

Adds:

* ``api_tokens`` table — long-lived PC-locked tokens for the ArcPy add-in.
  Each token carries a ``description`` and ``author`` (human-readable labels),
  a SHA-256 hash of the raw secret, and a ``machine_fingerprint`` that is set
  on first use and never changed thereafter. Any connect attempt from a
  different machine is rejected 403.

* ``request_log.chat_id`` — nullable string column populated from the
  ``X-Chat-Id`` request header. Groups all steps of a single agent
  invocation into one logical conversation for log queries.
  Index ``ix_request_log_chat_id`` for fast per-chat queries.

Downgrade drops both additions without touching existing tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- api_tokens table ------------------------------------------------
    op.create_table(
        "api_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("machine_fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])

    # ---- request_log.chat_id column + index ------------------------------
    op.add_column(
        "request_log",
        sa.Column("chat_id", sa.String(), nullable=True),
    )
    op.create_index("ix_request_log_chat_id", "request_log", ["chat_id"])


def downgrade() -> None:
    op.drop_index("ix_request_log_chat_id", table_name="request_log")
    op.drop_column("request_log", "chat_id")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")
