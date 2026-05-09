"""Tests for ``gateway.ratelimit``.

These exercise the Lua token-bucket against a real Redis (per
CLAUDE.md — no mocks for stateful infra). When Redis isn't reachable
at ``REDIS_URL`` the ``redis_client`` fixture skips the whole
module.

Coverage:

* First-request-allowed happy path.
* Capacity exhaustion → 429 shape with ``Retry-After``.
* Refill over (Redis-side) time advancement.
* Per-user bucket isolation.
* Atomicity under concurrent ``asyncio.gather`` — this is the test
  that earns the Lua script its keep.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from gateway.ratelimit import check_rate_limit, check_user_rate_limit


# ---- Bucket-level (low-level API) ----------------------------------------


async def test_rate_limit_allows_first_request(redis_client) -> None:  # type: ignore[no-untyped-def]
    """A fresh bucket starts at full capacity and the first call succeeds."""
    result = await check_rate_limit(
        redis_client,
        key=f"rl:test:{uuid4()}",
        capacity=5,
        refill_per_sec=1.0,
    )
    assert result.allowed is True
    assert result.retry_after_sec == 0
    assert result.remaining == 4  # one deducted from capacity=5


async def test_rate_limit_blocks_after_capacity_exceeded(redis_client) -> None:  # type: ignore[no-untyped-def]
    """capacity=3, fire 4 — fourth must 429 with a positive Retry-After."""
    key = f"rl:test:{uuid4()}"

    for i in range(3):
        result = await check_rate_limit(
            redis_client, key=key, capacity=3, refill_per_sec=0.5
        )
        assert result.allowed is True, f"call {i} should succeed"

    # Fourth: bucket empty.
    result = await check_rate_limit(
        redis_client, key=key, capacity=3, refill_per_sec=0.5
    )
    assert result.allowed is False
    assert result.retry_after_sec >= 1
    # remaining floor at 0 (we reject without deducting on the failure
    # path).
    assert result.remaining == 0


async def test_rate_limit_refills_over_time(redis_client) -> None:  # type: ignore[no-untyped-def]
    """After exhausting the bucket, advancing time refills tokens.

    We can't (and shouldn't) freeze Redis's ``TIME``, so instead of
    waiting in real time we manipulate the stored ``ts`` to back-date
    the last refill. This is the same effect a sleep would have, but
    deterministic.
    """
    key = f"rl:test:{uuid4()}"
    capacity = 2
    refill_per_sec = 1.0

    # Drain.
    for _ in range(capacity):
        await check_rate_limit(
            redis_client, key=key, capacity=capacity, refill_per_sec=refill_per_sec
        )
    blocked = await check_rate_limit(
        redis_client, key=key, capacity=capacity, refill_per_sec=refill_per_sec
    )
    assert blocked.allowed is False

    # Back-date ts by 10 seconds — at refill=1/s that's 10 tokens, but
    # the bucket caps at ``capacity``, so we expect a full refill.
    raw_ts = await redis_client.hget(key, "ts")
    ts = float(raw_ts)
    await redis_client.hset(key, "ts", str(ts - 10.0))

    refilled = await check_rate_limit(
        redis_client, key=key, capacity=capacity, refill_per_sec=refill_per_sec
    )
    assert refilled.allowed is True
    # After deducting one token from a refilled-to-cap bucket, remaining
    # is capacity - 1.
    assert refilled.remaining == capacity - 1


async def test_rate_limit_separate_users_separate_buckets(redis_client) -> None:  # type: ignore[no-untyped-def]
    """User A exhausting their bucket must not affect user B."""
    key_a = f"rl:test:{uuid4()}"
    key_b = f"rl:test:{uuid4()}"

    # Drain A.
    for _ in range(3):
        await check_rate_limit(
            redis_client, key=key_a, capacity=3, refill_per_sec=0.1
        )
    a_blocked = await check_rate_limit(
        redis_client, key=key_a, capacity=3, refill_per_sec=0.1
    )
    assert a_blocked.allowed is False

    # B is untouched.
    b_first = await check_rate_limit(
        redis_client, key=key_b, capacity=3, refill_per_sec=0.1
    )
    assert b_first.allowed is True
    assert b_first.remaining == 2


async def test_rate_limit_atomic_under_concurrency(redis_client) -> None:  # type: ignore[no-untyped-def]
    """20 concurrent calls against capacity=5 → exactly 5 allowed.

    This is the test that justifies the Lua script. With a naïve
    GET/SET pair, several concurrent calls could each see ``tokens=5``
    before any of them write back, and we'd silently allow more than
    capacity. The Lua path runs as one atomic command — exactly
    capacity should pass.
    """
    key = f"rl:test:{uuid4()}"
    capacity = 5

    results = await asyncio.gather(
        *[
            check_rate_limit(
                redis_client,
                key=key,
                capacity=capacity,
                # Slow refill so no tokens trickle in during the
                # gather window — keeps the assertion exact.
                refill_per_sec=0.001,
            )
            for _ in range(20)
        ]
    )
    allowed_count = sum(1 for r in results if r.allowed)
    blocked_count = sum(1 for r in results if not r.allowed)
    assert allowed_count == capacity, (
        f"expected exactly {capacity} allowed, got {allowed_count}"
    )
    assert blocked_count == 20 - capacity


# ---- User-level (settings wrapper) ---------------------------------------


async def test_check_user_rate_limit_uses_settings(redis_client) -> None:  # type: ignore[no-untyped-def]
    """The convenience wrapper sizes the bucket from settings."""
    from gateway.config import get_settings

    settings = get_settings()
    user_id = uuid4()

    result = await check_user_rate_limit(redis_client, user_id, settings)
    assert result.allowed is True
    # Bucket capacity is rate_limit_per_min — first call leaves cap-1.
    assert result.remaining == settings.rate_limit_per_min - 1

    # Verify the key shape: ``rl:user:<uuid>``.
    raw_tokens = await redis_client.hget(f"rl:user:{user_id}", "tokens")
    assert raw_tokens is not None


async def test_evalsha_falls_back_to_eval_on_noscript(redis_client) -> None:  # type: ignore[no-untyped-def]
    """SCRIPT FLUSH simulates a Redis restart; the next call must still work.

    The fallback path in ``check_rate_limit`` catches NOSCRIPT and
    re-runs as plain EVAL. Without it, every Redis upgrade or
    ``SCRIPT FLUSH`` would brick rate limiting until the app
    restarted.
    """
    from gateway.ratelimit import _SHA_ATTR

    key = f"rl:test:{uuid4()}"
    # Prime the script cache.
    await check_rate_limit(redis_client, key=key, capacity=3, refill_per_sec=1.0)

    # Now flush the server-side cache. The SHA we cached on the client
    # is stale.
    await redis_client.script_flush()

    # Second call: EVALSHA fails with NOSCRIPT, code falls back to
    # EVAL, succeeds.
    result = await check_rate_limit(
        redis_client, key=key, capacity=3, refill_per_sec=1.0
    )
    assert result.allowed is True

    # And the cache should be re-populated (best-effort) so subsequent
    # calls take the fast path again.
    cached = getattr(redis_client, _SHA_ATTR, None)
    assert cached  # not None or empty


# ---- HTTP integration via the FastAPI dep --------------------------------


async def test_messages_endpoint_returns_429_when_rate_limited(
    auth_client,  # type: ignore[no-untyped-def]
    openrouter_mock,  # type: ignore[no-untyped-def]
    redis_client,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: drain the bucket, next /v1/messages call gets 429.

    The DB-backed fixtures don't share state with ``redis_client``
    out-of-the-box (the app's lifespan opened its own Redis client),
    but they DO point at the same ``REDIS_URL``, so we can exhaust
    the bucket via direct Redis writes that the in-app client will
    observe on the next request.
    """
    client, headers, user_info = auth_client
    user_id = user_info["id"]

    # Force a tiny capacity for this test by overriding the dep at
    # the FastAPI level. We do this by monkey-patching the settings
    # singleton's rate_limit_per_min — the dep reads it on every
    # call, so the change takes effect immediately.
    from gateway.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_min", 2)

    # Pre-drain the bucket via a direct Redis write so the next
    # request from the app definitely hits the empty state.
    bucket_key = f"rl:user:{user_id}"
    await redis_client.hset(bucket_key, mapping={"tokens": "0", "ts": "9999999999"})
    await redis_client.expire(bucket_key, 300)

    # Hit /v1/usage (which is NOT rate-limited per the spec) to confirm
    # that decision: still 200 even though the bucket is empty.
    usage = await client.get("/v1/usage", headers=headers)
    assert usage.status_code == 200, usage.text

    # Hit /v1/messages — this IS rate-limited.
    resp = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "rate_limited"
    assert body["detail"]["retry_after_sec"] >= 1
    assert resp.headers.get("retry-after") is not None
