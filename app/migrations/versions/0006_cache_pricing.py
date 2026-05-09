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

Upgrade also seeds Anthropic prompt-cache prices for the three models
already present in ``model_pricing`` (opus / sonnet / haiku). Other rows
keep NULL prices and skip the cache surcharge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


# Anthropic prompt-cache list prices (USD / Mtoken) at the time this
# migration was authored. Rows are inserted only if the matching model
# already exists in ``model_pricing``; we do not invent new rows here.
_CACHE_SEED: tuple[tuple[str, str, str], ...] = (
    # (model, cache_read_per_mtoken, cache_write_per_mtoken)
    ("anthropic/claude-opus-4.7", "1.5000", "18.7500"),
    ("anthropic/claude-sonnet-4.6", "0.3000", "3.7500"),
    ("anthropic/claude-haiku-4.5", "0.1000", "1.2500"),
)


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

    bind = op.get_bind()
    for model, read_p, write_p in _CACHE_SEED:
        bind.execute(
            sa.text(
                "UPDATE model_pricing "
                "SET cache_read_per_mtoken = :r, cache_write_per_mtoken = :w "
                "WHERE model = :m"
            ),
            {"m": model, "r": read_p, "w": write_p},
        )


def downgrade() -> None:
    op.drop_column("request_log", "cache_write_tokens")
    op.drop_column("request_log", "cache_read_tokens")
    op.drop_column("model_pricing", "cache_write_per_mtoken")
    op.drop_column("model_pricing", "cache_read_per_mtoken")
