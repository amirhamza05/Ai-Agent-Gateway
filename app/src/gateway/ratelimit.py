"""Redis-backed token-bucket rate limiter.

Per §9 of the plan: every authenticated request to ``/v1/messages`` (and
later ``/v1/embeddings``, ``/v1/vectors/*``) is gated by a per-user QPM
budget. We use a token bucket because it tolerates short bursts while
holding the long-run average — the right shape for a chat client that
sends a flurry on agent-iteration boundaries and then idles.

Atomicity matters here. A naïve "GET tokens; compute; SET tokens"
sequence loses updates under concurrent traffic from the same user
(e.g. the add-in firing several tool calls in parallel). We push the
whole top-up + deduct + commit cycle into a Lua script so Redis runs
it as a single command — no read-modify-write race possible.

The bucket state is stored as a Redis hash with two fields: ``tokens``
(float, current bucket level) and ``ts`` (float, unix-seconds of the
last refill). We re-derive elapsed time from Redis's ``TIME`` rather
than trusting the client clock, so a misconfigured app server can't
hand out free tokens.

Idle-user TTL: every successful EVAL writes ``EXPIRE`` on the key for
``2 * (capacity / refill_per_sec)`` seconds. After two full bucket
refills with no traffic, the key auto-evicts and the next request
finds a full bucket — same outcome the slow path would have given
us, with no allocator pressure on Redis.

Public API: :func:`check_rate_limit` for the generic call,
:func:`check_user_rate_limit` for the convenience wrapper that the
FastAPI dependency in :mod:`gateway.limits` reaches for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

    from gateway.config import Settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a single bucket check.

    * ``allowed`` — ``True`` when ``cost`` tokens were deducted.
    * ``retry_after_sec`` — non-zero only when ``allowed=False``; how
      long the caller should wait before its next attempt would
      succeed (ceil to a full second so headers stay integer-valued).
    * ``remaining`` — integer tokens still in the bucket (post-deduct
      on success, pre-deduct floor on failure).
    * ``reset_at`` — unix-seconds at which the bucket would next reach
      full capacity assuming no further traffic. Useful for surfacing
      a "limit lifts at HH:MM" message in the add-in.
    """

    allowed: bool
    retry_after_sec: int
    remaining: int
    reset_at: int


# Lua script. Keys: bucket hash. ARGV: capacity, refill_per_sec, cost,
# ttl_seconds. Returns: {allowed, retry_after_sec, remaining, reset_at}.
#
# We pull "now" from Redis's TIME so all app workers share one clock —
# otherwise a worker with skewed time could either gift or starve a
# user. ``redis.call("TIME")`` returns ``{seconds, microseconds}`` as
# strings; we promote both to numbers and combine to a fractional
# second.
_LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local time = redis.call("TIME")
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

local stored = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(stored[1])
local ts = tonumber(stored[2])

if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end

-- Top up: tokens accumulate at `refill` per second, capped at capacity.
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed
local retry_after
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
  retry_after = 0
else
  allowed = 0
  -- Ceil to whole seconds so the Retry-After header stays integer.
  local needed = cost - tokens
  retry_after = math.ceil(needed / refill)
  if retry_after < 1 then retry_after = 1 end
end

redis.call("HSET", key, "tokens", tokens, "ts", now)
redis.call("EXPIRE", key, ttl)

-- reset_at: when the bucket would reach full capacity at the current
-- refill rate. Used by clients to render "limit lifts at HH:MM".
local deficit = capacity - tokens
local reset_at
if deficit <= 0 then
  reset_at = math.floor(now)
else
  reset_at = math.floor(now + deficit / refill)
end

-- Floor remaining so the JSON shape stays integer-valued for the
-- header. Fractional partial-tokens are an internal concern.
return {allowed, retry_after, math.floor(tokens), reset_at}
"""


# Attribute name used to cache the EVALSHA digest on the redis client
# instance. Stashing it on the client (rather than a module global)
# keeps us safe across multiple Redis instances in the same process —
# tests instantiate fresh clients and shouldn't share script cache
# state across them.
_SHA_ATTR = "_gw_ratelimit_sha"


async def _ensure_script_loaded(redis: Redis) -> str:
    """Return the SHA1 of the loaded Lua script, loading it on first use.

    EVALSHA fails with ``NOSCRIPT`` when Redis was restarted (or
    flushed) since we last loaded; the caller falls back to plain EVAL
    in that case and re-caches the SHA on the next call.
    """
    cached = getattr(redis, _SHA_ATTR, None)
    if cached:
        return str(cached)
    sha = await redis.script_load(_LUA_TOKEN_BUCKET)
    sha_str = sha.decode("utf-8") if isinstance(sha, bytes) else str(sha)
    setattr(redis, _SHA_ATTR, sha_str)
    return sha_str


async def check_rate_limit(
    redis: Redis,
    *,
    key: str,
    capacity: int,
    refill_per_sec: float,
    cost: int = 1,
) -> RateLimitResult:
    """Atomically deduct ``cost`` from the token bucket at ``key``.

    The bucket is created lazily on first contact with ``capacity``
    full tokens. Subsequent calls top up at ``refill_per_sec`` per
    second up to ``capacity``.

    Args:
        redis: An async Redis client (``redis.asyncio.Redis``).
        key: Redis key for the bucket hash. Caller's responsibility to
            namespace appropriately (e.g. ``rl:user:<uuid>``).
        capacity: Maximum tokens the bucket can hold.
        refill_per_sec: Tokens added per second when below capacity.
        cost: Tokens to deduct on a successful call. Defaults to 1
            (one request = one token); larger costs allow weighted
            endpoints (e.g. embeddings might cost 5).

    Returns:
        A :class:`RateLimitResult`. ``allowed=True`` iff the bucket
        had enough tokens; otherwise the deduction is skipped and
        ``retry_after_sec`` indicates how long until ``cost`` tokens
        would be available again.
    """
    # 2× the time-to-fully-refill is enough to keep an active user's
    # bucket alive between requests while letting truly idle users
    # auto-evict. Below 60 s we'd churn keys for the noisiest users.
    if refill_per_sec <= 0:
        raise ValueError("refill_per_sec must be > 0")
    ttl_seconds = max(60, int(2 * (capacity / refill_per_sec)))

    args = [str(capacity), str(refill_per_sec), str(cost), str(ttl_seconds)]

    try:
        sha = await _ensure_script_loaded(redis)
        raw = await redis.evalsha(sha, 1, key, *args)
    except Exception as exc:
        # NOSCRIPT means the script was flushed (Redis restart, manual
        # SCRIPT FLUSH, etc.). Re-load by EVAL'ing the source — Redis
        # caches it on first EVAL too — and refresh our SHA.
        message = str(exc).lower()
        if "noscript" not in message and "no matching script" not in message:
            raise
        if hasattr(redis, _SHA_ATTR):
            try:
                delattr(redis, _SHA_ATTR)
            except AttributeError:  # pragma: no cover
                pass
        raw = await redis.eval(_LUA_TOKEN_BUCKET, 1, key, *args)
        # Best-effort: cache the now-loaded SHA so subsequent calls
        # use the cheaper EVALSHA path again.
        try:
            await _ensure_script_loaded(redis)
        except Exception:  # pragma: no cover
            pass

    # Redis returns a list of strings/ints depending on the client's
    # decoding mode. Normalise to plain ints.
    allowed_raw, retry_raw, remaining_raw, reset_raw = raw
    return RateLimitResult(
        allowed=int(allowed_raw) == 1,
        retry_after_sec=int(retry_raw),
        remaining=int(remaining_raw),
        reset_at=int(reset_raw),
    )


async def check_user_rate_limit(
    redis: Redis,
    user_id: UUID,
    settings: Settings,
) -> RateLimitResult:
    """Convenience wrapper: per-user bucket sized from ``settings``.

    Key: ``rl:user:<uuid>``. Capacity matches ``RATE_LIMIT_PER_MIN``
    so a user can burst the whole minute's budget at once and then
    refill at the steady-state rate (``rate_limit_per_min / 60`` per
    second). That shape is friendlier to chat-style traffic than a
    fixed-window counter would be.
    """
    capacity = max(1, int(settings.rate_limit_per_min))
    refill_per_sec = float(capacity) / 60.0
    return await check_rate_limit(
        redis,
        key=f"rl:user:{user_id}",
        capacity=capacity,
        refill_per_sec=refill_per_sec,
        cost=1,
    )
