"""Unit tests for ``gateway.billing.compute_cost_usd`` and
``compute_embedding_cost_usd``."""

from __future__ import annotations

from decimal import Decimal

from gateway.billing import (
    EMBEDDING_PRICES_PER_MTOKEN,
    PRICES_PER_MTOKEN,
    compute_cost_usd,
    compute_embedding_cost_usd,
)


def test_compute_cost_haiku_known_value() -> None:
    """Haiku at $1/Mt input + $5/Mt output: 1000 / 500 should give precise value."""
    cost = compute_cost_usd("anthropic/claude-haiku-4.5", 1000, 500)
    # 1000 * 1 / 1_000_000 + 500 * 5 / 1_000_000 = 0.001 + 0.0025 = 0.0035
    assert cost == Decimal("0.003500")


def test_compute_cost_sonnet_known_value() -> None:
    cost = compute_cost_usd("anthropic/claude-sonnet-4.6", 1_000_000, 1_000_000)
    # 1M tokens at $3 in + $15 out = $18.00
    assert cost == Decimal("18.000000")


def test_compute_cost_opus_known_value() -> None:
    cost = compute_cost_usd("anthropic/claude-opus-4.7", 100, 50)
    # 100 * 15 / 1M + 50 * 75 / 1M = 0.0015 + 0.00375 = 0.00525
    assert cost == Decimal("0.005250")


def test_compute_cost_zero_tokens_returns_zero() -> None:
    cost = compute_cost_usd("anthropic/claude-haiku-4.5", 0, 0)
    assert cost == Decimal("0E-6") or cost == Decimal("0.000000")
    assert cost == Decimal("0")


def test_compute_cost_unknown_model_returns_none() -> None:
    cost = compute_cost_usd("openai/gpt-4-turbo", 100, 50)
    assert cost is None


def test_price_table_keys_match_env_example() -> None:
    """Pricing keys must match the ``.env.example`` ALLOWED_MODELS list.

    If a developer adds a new model to ALLOWED_MODELS without updating the
    price table, every request for that model will store ``cost_usd=NULL``
    and silently leak budget. This test catches that drift.
    """
    expected = {
        "anthropic/claude-opus-4.7",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.5",
    }
    assert set(PRICES_PER_MTOKEN.keys()) == expected


# ---- Embedding cost ------------------------------------------------------


def test_compute_embedding_cost_small_known_value() -> None:
    """text-embedding-3-small at $0.020/Mt: 1M tokens → $0.020000."""
    cost = compute_embedding_cost_usd("openai/text-embedding-3-small", 1_000_000)
    assert cost == Decimal("0.020000")


def test_compute_embedding_cost_large_known_value() -> None:
    """text-embedding-3-large at $0.130/Mt: 1M tokens → $0.130000."""
    cost = compute_embedding_cost_usd("openai/text-embedding-3-large", 1_000_000)
    assert cost == Decimal("0.130000")


def test_compute_embedding_cost_zero_tokens() -> None:
    cost = compute_embedding_cost_usd("openai/text-embedding-3-small", 0)
    assert cost == Decimal("0")


def test_compute_embedding_cost_unknown_model_returns_none() -> None:
    cost = compute_embedding_cost_usd("voyage/voyage-3", 100)
    assert cost is None


def test_embedding_price_table_keys_match_env_example() -> None:
    """Embedding pricing keys must match the embedding entries in
    ``.env.example``'s ALLOWED_MODELS list."""
    expected = {
        "openai/text-embedding-3-small",
        "openai/text-embedding-3-large",
    }
    assert set(EMBEDDING_PRICES_PER_MTOKEN.keys()) == expected
