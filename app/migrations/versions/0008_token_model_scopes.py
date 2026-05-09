"""api token <-> model scope link

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-10 16:00:00.000000

Adds:

* ``api_tokens.allow_all_models`` — boolean, default TRUE. When TRUE the
  token may use every currently-allowed model (and automatically picks
  up future ones added via the dashboard). When FALSE, the token is
  restricted to the rows in ``api_token_models``.

* ``api_token_models`` — many-to-many join between ``api_tokens`` and
  ``model_pricing``. Composite PK ``(token_id, model)`` so duplicates
  are rejected at the DB level. Both sides cascade-delete: dropping a
  token or dropping a pricing row also drops the link rows.

Existing tokens default to ``allow_all_models=true`` so the migration
preserves the prior "any allowed model" behaviour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_tokens",
        sa.Column(
            "allow_all_models",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )

    op.create_table(
        "api_token_models",
        sa.Column("token_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["token_id"], ["api_tokens.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model"], ["model_pricing.model"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_id", "model"),
    )
    op.create_index(
        "ix_api_token_models_token_id", "api_token_models", ["token_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_token_models_token_id", table_name="api_token_models")
    op.drop_table("api_token_models")
    op.drop_column("api_tokens", "allow_all_models")
