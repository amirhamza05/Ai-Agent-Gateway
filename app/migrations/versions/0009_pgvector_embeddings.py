"""pgvector embeddings table

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-05-14 00:00:00.000000

Adds:

* ``CREATE EXTENSION IF NOT EXISTS vector`` — installs the pgvector extension.
  The Postgres image must be ``pgvector/pgvector:pg16`` (not stock
  ``postgres:16``) for this to succeed. See §5 of the migration plan.

* ``embeddings`` table — stores 1536-dimensional vector embeddings with an
  arbitrary JSONB payload, replacing the Qdrant Cloud backend. Upsert key
  is ``(collection, point_id)``; unique constraint named
  ``uq_embeddings_collection_point`` enforces it at the DB level.

* Three indexes:
  - ``embeddings_collection_idx`` — btree on ``collection`` for per-collection
    filtering on every search and upsert call.
  - ``embeddings_payload_gin`` — GIN with ``jsonb_path_ops`` on ``payload``
    for Qdrant-compatible filter queries (``payload @> '{"key": "val"}'``).
  - ``embeddings_hnsw_cos`` — HNSW on ``embedding`` using
    ``vector_cosine_ops`` (m=16, ef_construction=64). Must be created via
    ``op.execute`` because Alembic's ``op.create_index`` cannot express HNSW
    storage parameters.

downgrade() drops the three indexes and the table but does NOT drop the
extension — other queries or future tables may depend on it.

Schema-only migration. No seed data. Real backfill comes from the
``gateway-admin migrate-qdrant`` CLI (§7 of the migration plan).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Ensure the pgvector extension is present. Idempotent — safe to run
    #    even if a previous failed migration attempt already created it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. Create the embeddings table.
    op.create_table(
        "embeddings",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            primary_key=True,
        ),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("point_id", sa.Text(), nullable=False),
        # Vector(1536): pgvector column type from pgvector.sqlalchemy.
        # Dimension is fixed at 1536 — covers OpenAI text-embedding-3-small
        # and Voyage voyage-3-lite. See §4 schema notes of the migration plan.
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.UniqueConstraint(
            "collection", "point_id", name="uq_embeddings_collection_point"
        ),
    )

    # 3a. Btree index on collection — hit by every filtered search/upsert.
    op.create_index("embeddings_collection_idx", "embeddings", ["collection"])

    # 3b. GIN index on payload — used by the filter translator for
    #     containment queries: ``payload @> '{"doc_type": "manual"}'``.
    op.create_index(
        "embeddings_payload_gin",
        "embeddings",
        ["payload"],
        postgresql_using="gin",
        postgresql_ops={"payload": "jsonb_path_ops"},
    )

    # 3c. HNSW index for approximate nearest-neighbour cosine search.
    #     op.create_index cannot express HNSW storage parameters, so we use
    #     raw SQL. m=16 and ef_construction=64 match Qdrant Cloud defaults.
    op.execute(
        "CREATE INDEX embeddings_hnsw_cos ON embeddings "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    # Drop indexes first (Postgres requires it before dropping the table when
    # the index references the table, though it would cascade anyway — be explicit).
    op.execute("DROP INDEX IF EXISTS embeddings_hnsw_cos")
    op.drop_index("embeddings_payload_gin", table_name="embeddings")
    op.drop_index("embeddings_collection_idx", table_name="embeddings")
    op.drop_table("embeddings")
    # Intentionally NOT dropping the vector extension. Other objects
    # (or a re-run of upgrade()) may depend on it. Remove it manually if needed.
