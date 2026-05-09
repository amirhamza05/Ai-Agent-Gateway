"""cache pricing + cache token columns

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-10 12:00:00.000000

Adds:

* ``model_pricing.cache_read_per_mtoken`` and
  ``model_pricing.cache_write_per_mtoken`` — USD per million tokens for
  Anthropic prompt-cache reads (cheap, ~0.10× input) and writes
  (premium, ~1.25× input). Both nullable so embedding rows are unaffected.
* ``request_log.cache_read_tokens`` and ``request_log.cache_write_tokens``
  — per-request cache hit / cache write token counts as parsed from
  Anthropic's ``usage`` object.

The columns are added empty; cache prices are filled in by the operator
from /dashboard/models when they enter pricing for each model.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_pricing",
        sa.Column("cache_read_per_mtoken", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "model_pricing",
        sa.Column("cache_write_per_mtoken", sa.Numeric(10, 4), nullable=True),
    )

    op.add_column(
        "request_log",
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "request_log",
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("request_log", "cache_write_tokens")
    op.drop_column("request_log", "cache_read_tokens")
    op.drop_column("model_pricing", "cache_write_per_mtoken")
    op.drop_column("model_pricing", "cache_read_per_mtoken")
