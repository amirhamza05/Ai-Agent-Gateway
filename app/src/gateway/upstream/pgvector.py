"""pgvector backend for ``/v1/vectors/search`` and ``/v1/vectors/upsert``.

All public functions return JSON shaped as ``{"result": ..., "status": ...,
"time": ...}``.

Supported filter DSL
--------------------
Only the shapes actually used by ``SearchGeoswmmDocsTool`` are implemented.
Anything outside this subset raises ``ValueError("invalid_filter")`` which
the route layer converts to ``400 {"error": "invalid_filter"}``.

Supported::

    {"must": [{"key": "doc_type", "match": {"value": "manual"}}]}
    {"must": [{"key": "year",    "range": {"gte": 2020, "lte": 2024}}]}
    {"must_not": [{"key": "hidden", "match": {"value": True}}]}

Combinations of ``must`` + ``must_not`` at the same level are allowed and
combined with ``and_()``. Nesting (``should``, nested ``must``) is not
supported and raises ``ValueError("invalid_filter")``.

Distance / score
----------------
Uses cosine distance (``vector_cosine_ops`` HNSW index). Score is
``1 - cosine_distance`` — i.e. cosine similarity, range roughly [−1, 1]
for un-normalised vectors, [0, 1] for unit-normalised ones.
"""

from __future__ import annotations

import re
import time
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from gateway.db.models import Embedding


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Maximum rows returned by a single search; also enforced by the Pydantic
# model upstream, but we guard here too so this function is safe standalone.
_MAX_LIMIT = 200

# hnsw.ef_search controls the recall/speed tradeoff for HNSW ANN queries.
_EF_SEARCH = 64

# Strict allow-pattern for collection names: alphanumerics, underscore,
# dash. Anchored so partial matches (e.g. ``foo/../bar``) are rejected.
# Capped at 64 chars so a misuse can't dump a megabyte into a query.
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def validate_collection(name: str) -> None:
    """Reject collection names that don't match the strict allow-pattern.

    Raises :class:`fastapi.HTTPException` with 400 +
    ``{"error": "invalid_collection_name"}`` so the route layer doesn't
    need to repeat this check.
    """
    if not _COLLECTION_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_collection_name"},
        )


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------


def _translate_filter(filter_dict: dict[str, Any]) -> list[ColumnElement]:  # type: ignore[type-arg]
    """Translate a filter dict to SQLAlchemy WHERE clauses.

    Returns a flat list of ``ColumnElement`` objects intended to be
    unpacked into ``.where(*clauses)``. An empty filter dict returns
    an empty list (no restriction).

    Raises ``ValueError("invalid_filter")`` for any shape we don't support
    so the route can surface a deterministic 400 rather than silently
    ignoring an unrecognised constraint.
    """
    if not filter_dict:
        return []

    known_keys = {"must", "must_not"}
    unknown = set(filter_dict) - known_keys
    if unknown:
        raise ValueError("invalid_filter")

    clauses: list[ColumnElement] = []  # type: ignore[type-arg]

    for top_key in ("must", "must_not"):
        conditions = filter_dict.get(top_key)
        if conditions is None:
            continue
        if not isinstance(conditions, list):
            raise ValueError("invalid_filter")

        inner: list[ColumnElement] = []  # type: ignore[type-arg]
        for cond in conditions:
            if not isinstance(cond, dict):
                raise ValueError("invalid_filter")
            if set(cond.keys()) != {"key", "match"} and set(cond.keys()) != {"key", "range"}:
                raise ValueError("invalid_filter")

            field_key: str = cond["key"]
            if not isinstance(field_key, str) or not field_key:
                raise ValueError("invalid_filter")

            if "match" in cond:
                clause = _translate_match(field_key, cond["match"])
            else:
                clause = _translate_range(field_key, cond["range"])

            inner.append(clause)

        if not inner:
            continue

        combined = sa.and_(*inner) if len(inner) > 1 else inner[0]
        if top_key == "must_not":
            combined = sa.not_(combined)
        clauses.append(combined)

    return clauses


def _translate_match(field_key: str, match: Any) -> ColumnElement:  # type: ignore[type-arg]
    """``{"match": {"value": V}}`` → ``payload @> '{"field_key": V}'``."""
    if not isinstance(match, dict) or "value" not in match or len(match) != 1:
        raise ValueError("invalid_filter")
    value = match["value"]
    # GIN containment via the `@>` operator. Bind a Python dict and let
    # SQLAlchemy's JSONB type adapt it once — wrapping in json.dumps first
    # double-encodes (asyncpg's JSONB processor JSON-encodes again), which
    # produces a stringified JSON literal that `@>` never matches.
    return Embedding.payload.op("@>")(sa.type_coerce({field_key: value}, JSONB))


def _translate_range(field_key: str, range_dict: Any) -> ColumnElement:  # type: ignore[type-arg]
    """``{"range": {"gte": N, ...}}`` → numeric comparisons on ``payload->>'field_key'``."""
    if not isinstance(range_dict, dict):
        raise ValueError("invalid_filter")

    allowed_ops = {"gte", "lte", "gt", "lt"}
    if not range_dict or not set(range_dict).issubset(allowed_ops):
        raise ValueError("invalid_filter")

    # Cast the text extraction to numeric so Postgres uses numeric comparisons.
    col = sa.cast(Embedding.payload[field_key].astext, sa.Numeric)

    parts: list[ColumnElement] = []  # type: ignore[type-arg]
    for op, val in range_dict.items():
        if op == "gte":
            parts.append(col >= val)
        elif op == "lte":
            parts.append(col <= val)
        elif op == "gt":
            parts.append(col > val)
        elif op == "lt":
            parts.append(col < val)

    return sa.and_(*parts) if len(parts) > 1 else parts[0]


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


async def search(
    session: AsyncSession,
    collection: str,
    vector: list[float],
    limit: int,
    filter: dict[str, Any] | None,  # noqa: A002  — mirrors the route param name
    with_payload: bool | dict[str, Any],
    score_threshold: float | None,
) -> dict[str, Any]:
    """Search ``collection`` for the nearest neighbours of ``vector``.

    Returns a dict::

        {
            "result": [{"id": ..., "score": ..., "payload": ...}, ...],
            "status": "ok",
            "time": <seconds as float>,
        }

    Args:
        session: Active async SQLAlchemy session.
        collection: Collection name (already validated by the route).
        vector: Query embedding as a Python list of floats.
        limit: Maximum number of results (capped at 200).
        filter: Optional filter dict. Translated via
            :func:`_translate_filter`; unsupported shapes raise ValueError.
        with_payload: If False, omit payload from results. If a dict with
            ``include`` / ``exclude`` keys, project accordingly.
        score_threshold: Minimum cosine similarity for a result to be
            included. Applied post-query in Python (simpler than HAVING
            on a computed label).

    Raises:
        ValueError: If ``filter`` contains unsupported filter DSL shapes.
    """
    limit = min(limit, _MAX_LIMIT)
    started = time.monotonic()

    filter_clauses = _translate_filter(filter or {})

    # Set HNSW recall tuning for this transaction. SET LOCAL is transaction-
    # scoped — it resets automatically on COMMIT/ROLLBACK, so it won't leak
    # between requests sharing the same connection.
    await session.execute(sa.text(f"SET LOCAL hnsw.ef_search = {_EF_SEARCH}"))

    distance_expr = Embedding.embedding.cosine_distance(vector)
    score_expr = (1 - distance_expr).label("score")

    stmt = (
        select(Embedding.point_id, Embedding.payload, score_expr)
        .where(Embedding.collection == collection)
        .order_by(distance_expr)
        .limit(limit)
    )
    if filter_clauses:
        stmt = stmt.where(*filter_clauses)

    rows = (await session.execute(stmt)).all()

    results: list[dict[str, Any]] = []
    for point_id, payload, score in rows:
        score_val = float(score)
        if score_threshold is not None and score_val < score_threshold:
            continue

        result_payload: Any
        if with_payload is False:
            result_payload = None
        elif isinstance(with_payload, dict):
            result_payload = _project_payload(payload, with_payload)
        else:
            # True or anything else truthy: return everything
            result_payload = payload

        results.append({"id": point_id, "score": score_val, "payload": result_payload})

    elapsed = time.monotonic() - started
    return {"result": results, "status": "ok", "time": elapsed}


async def upsert(
    session: AsyncSession,
    collection: str,
    points: list[dict[str, Any]],
) -> dict[str, Any]:
    """Upsert ``points`` into ``collection``.

    Returns a dict::

        {"operation_id": <int>, "status": "completed", "time": <seconds>}

    Each element of ``points`` must have ``id`` (str or int) and ``vector``
    (list of floats). ``payload`` is optional; defaults to ``{}``.

    Raises:
        ValueError: If any point is missing a ``vector`` field.
    """
    started = time.monotonic()

    rows: list[dict[str, Any]] = []
    for point in points:
        if "vector" not in point or point["vector"] is None:
            raise ValueError("missing_vector")
        vec = point["vector"]
        if not isinstance(vec, list):
            raise ValueError("missing_vector")

        rows.append(
            {
                "collection": collection,
                "point_id": str(point["id"]),
                "embedding": vec,
                "payload": point.get("payload") or {},
            }
        )

    stmt = pg_insert(Embedding).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["collection", "point_id"],
        set_={
            "embedding": stmt.excluded.embedding,
            "payload": stmt.excluded.payload,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.commit()

    elapsed = time.monotonic() - started
    # operation_id has no meaningful server value in Postgres; use 0 as a
    # stable sentinel. The add-in only checks `status`, not `operation_id`.
    return {"operation_id": 0, "status": "completed", "time": elapsed}


# ---------------------------------------------------------------------------
# Payload projection helper
# ---------------------------------------------------------------------------


def _project_payload(
    payload: dict[str, Any] | None,
    selector: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply an ``include``/``exclude`` selector to a payload dict.

    - ``{"include": ["a", "b"]}`` → keep only those keys.
    - ``{"exclude": ["a"]}`` → drop those keys, return the rest.
    - Both present: include wins.

    If ``payload`` is None or the selector is empty, returns payload as-is.
    """
    if payload is None:
        return None
    if not selector:
        return payload

    include: list[str] | None = selector.get("include")
    exclude: list[str] | None = selector.get("exclude")

    if include is not None:
        return {k: v for k, v in payload.items() if k in include}
    if exclude is not None:
        return {k: v for k, v in payload.items() if k not in exclude}
    return payload
