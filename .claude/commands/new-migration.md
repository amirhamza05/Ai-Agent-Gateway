---
description: Create a new Alembic migration via the db-engineer agent
argument-hint: <description of schema change>
---

# New migration

Dispatch the `db-engineer` agent to author an Alembic migration for: $ARGUMENTS

The agent will:

1. Update the SQLAlchemy models in `app/src/gateway/db/models.py` if needed.
2. Run `alembic revision --autogenerate` against the dev DB.
3. Inspect the generated file and clean up Alembic's mis-detections.
4. Show you the migration before applying.

Apply the migration only after you've reviewed it with `/migrate --up`.
