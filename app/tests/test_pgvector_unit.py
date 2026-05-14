"""Pure unit tests for the pgvector upstream module.

No database, no app, no fixtures beyond standard pytest.  These tests
exercise the filter translator, cosine-score mathematics, and the
collection-name validator in isolation.

All imports are deferred inside each test so the module can be collected
without a running Postgres connection.
"""

from __future__ import annotations

import math

import pytest


# ---------------------------------------------------------------------------
# _translate_filter — round-trip tests
# ---------------------------------------------------------------------------


def test_translate_filter_empty_dict_returns_no_clauses() -> None:
    from gateway.upstream.pgvector import _translate_filter

    clauses = _translate_filter({})
    assert clauses == []


def test_translate_filter_match_value_produces_jsonb_containment() -> None:
    """``must[match.value]`` must generate a ``@>`` clause.

    The bound parameter is a Python dict (asyncpg's JSONB codec encodes it
    once on the wire). Wrapping in ``json.dumps`` first would double-encode
    and the ``@>`` operator would match nothing.
    """
    from sqlalchemy.dialects import postgresql as pg
    from gateway.upstream.pgvector import _translate_filter

    clauses = _translate_filter({"must": [{"key": "doc_type", "match": {"value": "manual"}}]})
    assert len(clauses) == 1
    compiled = clauses[0].compile(dialect=pg.dialect())
    sql_text = str(compiled)
    assert "@>" in sql_text
    params = compiled.params
    assert any(
        v == {"doc_type": "manual"} for v in params.values()
    ), f"Expected raw dict payload in params, got: {params}"


def test_translate_filter_match_value_bool() -> None:
    """Boolean match values must be accepted (used for ``hidden: True``)."""
    from gateway.upstream.pgvector import _translate_filter

    clauses = _translate_filter({"must": [{"key": "hidden", "match": {"value": True}}]})
    assert len(clauses) == 1


def test_translate_filter_range_gte_produces_numeric_comparison() -> None:
    from sqlalchemy.dialects import postgresql as pg
    from gateway.upstream.pgvector import _translate_filter

    clauses = _translate_filter({"must": [{"key": "year", "range": {"gte": 2020}}]})
    assert len(clauses) == 1
    compiled = clauses[0].compile(dialect=pg.dialect())
    sql_text = str(compiled)
    assert ">=" in sql_text


def test_translate_filter_range_lte_produces_numeric_comparison() -> None:
    from sqlalchemy.dialects import postgresql as pg
    from gateway.upstream.pgvector import _translate_filter

    clauses = _translate_filter({"must": [{"key": "year", "range": {"lte": 2024}}]})
    assert len(clauses) == 1
    compiled = clauses[0].compile(dialect=pg.dialect())
    sql_text = str(compiled)
    assert "<=" in sql_text


def test_translate_filter_range_gt_and_lt() -> None:
    from sqlalchemy.dialects import postgresql as pg
    from gateway.upstream.pgvector import _translate_filter

    clauses = _translate_filter({"must": [{"key": "year", "range": {"gt": 2019, "lt": 2025}}]})
    assert len(clauses) == 1
    compiled = clauses[0].compile(dialect=pg.dialect())
    sql_text = str(compiled)
    assert ">" in sql_text
    assert "<" in sql_text


def test_translate_filter_range_all_four_ops() -> None:
    from gateway.upstream.pgvector import _translate_filter

    # Should not raise; all four ops are valid.
    clauses = _translate_filter(
        {"must": [{"key": "score", "range": {"gte": 0, "lte": 100, "gt": -1, "lt": 101}}]}
    )
    assert len(clauses) == 1


def test_translate_filter_must_not_negates() -> None:
    """``must_not`` must wrap the clause in ``NOT (...)``."""
    from sqlalchemy.dialects import postgresql as pg
    from gateway.upstream.pgvector import _translate_filter

    clauses = _translate_filter(
        {"must_not": [{"key": "hidden", "match": {"value": True}}]}
    )
    assert len(clauses) == 1
    compiled = clauses[0].compile(dialect=pg.dialect())
    sql_text = str(compiled).upper()
    assert "NOT" in sql_text


def test_translate_filter_must_and_must_not_combined() -> None:
    """``must`` + ``must_not`` at the same level produces two clauses."""
    from gateway.upstream.pgvector import _translate_filter

    clauses = _translate_filter(
        {
            "must": [{"key": "doc_type", "match": {"value": "manual"}}],
            "must_not": [{"key": "draft", "match": {"value": True}}],
        }
    )
    assert len(clauses) == 2


def test_translate_filter_unknown_top_key_raises_value_error() -> None:
    from gateway.upstream.pgvector import _translate_filter

    with pytest.raises(ValueError, match="invalid_filter"):
        _translate_filter({"should": [{"key": "x", "match": {"value": 1}}]})


def test_translate_filter_unknown_condition_keys_raise_value_error() -> None:
    from gateway.upstream.pgvector import _translate_filter

    with pytest.raises(ValueError, match="invalid_filter"):
        _translate_filter({"must": [{"key": "x", "fuzzy": {"value": "hi"}}]})


def test_translate_filter_non_list_must_raises_value_error() -> None:
    from gateway.upstream.pgvector import _translate_filter

    with pytest.raises(ValueError, match="invalid_filter"):
        _translate_filter({"must": "not-a-list"})


def test_translate_filter_missing_key_in_condition_raises() -> None:
    from gateway.upstream.pgvector import _translate_filter

    with pytest.raises(ValueError, match="invalid_filter"):
        _translate_filter({"must": [{"match": {"value": 1}}]})  # no "key"


def test_translate_filter_match_missing_value_key_raises() -> None:
    from gateway.upstream.pgvector import _translate_filter

    with pytest.raises(ValueError, match="invalid_filter"):
        _translate_filter({"must": [{"key": "x", "match": {"text": "hi"}}]})


def test_translate_filter_range_with_unknown_op_raises() -> None:
    from gateway.upstream.pgvector import _translate_filter

    with pytest.raises(ValueError, match="invalid_filter"):
        _translate_filter({"must": [{"key": "x", "range": {"neq": 5}}]})


def test_translate_filter_range_empty_dict_raises() -> None:
    from gateway.upstream.pgvector import _translate_filter

    with pytest.raises(ValueError, match="invalid_filter"):
        _translate_filter({"must": [{"key": "x", "range": {}}]})


# ---------------------------------------------------------------------------
# Cosine distance / score math
# ---------------------------------------------------------------------------


def test_cosine_distance_zero_for_identical_unit_vector() -> None:
    """
    1 - cosine_distance(v, v) == 1.0 for a unit vector.

    pgvector computes cosine_distance as  1 - cosine_similarity, so a vector
    against itself has distance 0 and score 1.0.  We verify the arithmetic here
    without touching Postgres — the vector maths is standard linear algebra.
    """
    import math as _math

    def cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = _math.sqrt(sum(x * x for x in a))
        mag_b = _math.sqrt(sum(y * y for y in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    # Unit vector along the first axis.
    v = [1.0] + [0.0] * 1535
    sim = cosine_similarity(v, v)
    assert math.isclose(sim, 1.0, rel_tol=1e-9), f"Expected 1.0, got {sim}"
    # Score formula: 1 - cosine_distance; cosine_distance = 1 - cosine_similarity
    # So score = cosine_similarity.
    score = sim  # same value
    assert math.isclose(score, 1.0, rel_tol=1e-9)


def test_cosine_score_orthogonal_vectors_is_zero() -> None:
    """Orthogonal vectors have cosine similarity 0 → score 0."""
    import math as _math

    def cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = _math.sqrt(sum(x * x for x in a))
        mag_b = _math.sqrt(sum(y * y for y in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    a = [1.0, 0.0]
    b = [0.0, 1.0]
    sim = cosine_similarity(a, b)
    assert math.isclose(sim, 0.0, abs_tol=1e-9), f"Expected 0.0, got {sim}"


def test_cosine_score_opposite_vectors_is_minus_one() -> None:
    """Anti-parallel vectors have cosine similarity -1."""
    import math as _math

    def cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = _math.sqrt(sum(x * x for x in a))
        mag_b = _math.sqrt(sum(y * y for y in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    sim = cosine_similarity(a, b)
    assert math.isclose(sim, -1.0, abs_tol=1e-9), f"Expected -1.0, got {sim}"


# ---------------------------------------------------------------------------
# validate_collection — regex enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid_name",
    [
        "docs",
        "my_collection",
        "my-collection",
        "Collection1",
        "a" * 64,          # max allowed length
        "ABC_DEF-123",
    ],
)
def test_validate_collection_accepts_valid_names(valid_name: str) -> None:
    from gateway.upstream.pgvector import validate_collection

    # Should not raise.
    validate_collection(valid_name)


@pytest.mark.parametrize(
    "bad_name",
    [
        "",               # empty
        "a" * 65,         # too long
        "../../etc",      # path traversal
        "name with space",
        "name/with/slash",
        "name;drop_table",
        "$weird",
        "col.dot",
    ],
)
def test_validate_collection_rejects_invalid_names(bad_name: str) -> None:
    from fastapi import HTTPException
    from gateway.upstream.pgvector import validate_collection

    with pytest.raises(HTTPException) as exc_info:
        validate_collection(bad_name)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "invalid_collection_name"
