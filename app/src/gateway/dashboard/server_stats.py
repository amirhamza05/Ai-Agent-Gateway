"""Server-status data collection for the ``/dashboard/server`` page.

These helpers answer "is the DB filling up, which table is eating
space, are we leaking Redis keys, how many active connections" — the
operational questions you'd otherwise log into ``psql`` to ask.

Each function does one query and returns plain dicts/dataclasses; the
route handler bundles them into a single template context.

The disk helper reports the **container's view** of its root
filesystem. On a single-VPS Compose deploy that maps to the host
disk, which is what the operator actually wants to see. Per-volume
sizes (Postgres pgdata, Redis dump) require Docker socket access and
are intentionally not surfaced here.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Database size & shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TableSize:
    name: str
    total_bytes: int
    table_bytes: int
    toast_bytes: int
    index_bytes: int
    row_estimate: int


@dataclass(frozen=True, slots=True)
class TopRow:
    log_id: int
    request_id: str
    created_at: Any
    endpoint: str | None
    user_email: str | None
    request_bytes: int | None
    response_bytes: int | None
    total_bytes: int


async def database_size(session: AsyncSession) -> dict[str, Any]:
    """Total size of the current Postgres database + table count.

    ``pg_database_size`` includes everything: heap, indexes, TOAST,
    free space map, visibility map, and WAL records still in the
    cluster's view of the DB. It is the closest single number to
    "what does Postgres consume on disk for this database".
    """
    result = await session.execute(
        text(
            """
            SELECT
              pg_database_size(current_database())                       AS db_bytes,
              (SELECT COUNT(*) FROM pg_stat_user_tables)                 AS table_count
            """
        )
    )
    row = result.one()
    return {
        "db_bytes": int(row.db_bytes or 0),
        "table_count": int(row.table_count or 0),
    }


async def table_sizes(session: AsyncSession) -> list[TableSize]:
    """Per-table size breakdown for every public-schema user table.

    Splits into:
      * ``table_bytes``  — heap (main fork)
      * ``toast_bytes``  — out-of-line storage for large rows; for
        ``request_log`` this is where compressed bodies live
      * ``index_bytes``  — sum of all indexes on the table
      * ``total_bytes``  — ``pg_total_relation_size``
      * ``row_estimate`` — ``reltuples`` from the planner; cheap, no
        sequential scan, accurate to the last ANALYZE.
    """
    result = await session.execute(
        text(
            """
            SELECT
              c.relname AS name,
              pg_total_relation_size(c.oid)                              AS total_bytes,
              pg_relation_size(c.oid, 'main')                            AS table_bytes,
              COALESCE(pg_total_relation_size(c.reltoastrelid), 0)       AS toast_bytes,
              pg_indexes_size(c.oid)                                     AS index_bytes,
              c.reltuples::bigint                                        AS row_estimate
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname = 'public'
            ORDER BY pg_total_relation_size(c.oid) DESC
            """
        )
    )
    return [
        TableSize(
            name=r.name,
            total_bytes=int(r.total_bytes or 0),
            table_bytes=int(r.table_bytes or 0),
            toast_bytes=int(r.toast_bytes or 0),
            index_bytes=int(r.index_bytes or 0),
            row_estimate=int(r.row_estimate or 0),
        )
        for r in result.all()
    ]


async def connection_stats(session: AsyncSession) -> dict[str, Any]:
    """``pg_stat_activity`` snapshot.

    Tracks how Postgres connections are split between active queries
    and idlers, plus the wall-clock age of the longest-running query.
    A long ``max_query_seconds`` is the canary for runaway analytics
    or a stuck transaction holding locks.
    """
    result = await session.execute(
        text(
            """
            SELECT
              COUNT(*) FILTER (WHERE state = 'active')                          AS active,
              COUNT(*) FILTER (WHERE state = 'idle')                            AS idle,
              COUNT(*) FILTER (WHERE state = 'idle in transaction')             AS idle_in_tx,
              COUNT(*)                                                          AS total,
              COALESCE(EXTRACT(EPOCH FROM MAX(now() - query_start)
                       FILTER (WHERE state = 'active')), 0)                     AS max_query_seconds,
              COALESCE(EXTRACT(EPOCH FROM MAX(now() - xact_start)
                       FILTER (WHERE xact_start IS NOT NULL)), 0)               AS max_tx_seconds
            FROM pg_stat_activity
            WHERE datname = current_database()
            """
        )
    )
    row = result.one()
    return {
        "active": int(row.active or 0),
        "idle": int(row.idle or 0),
        "idle_in_tx": int(row.idle_in_tx or 0),
        "total": int(row.total or 0),
        "max_query_seconds": float(row.max_query_seconds or 0.0),
        "max_tx_seconds": float(row.max_tx_seconds or 0.0),
    }


async def cache_hit_ratio(session: AsyncSession) -> dict[str, Any]:
    """Heap and index buffer cache hit ratios from ``pg_stat_database``.

    A healthy busy DB stays above ~0.99 for both. A sustained drop
    below that points at either a hot working set that has outgrown
    ``shared_buffers`` or a missing index.
    """
    result = await session.execute(
        text(
            """
            SELECT
              blks_hit, blks_read,
              CASE WHEN blks_hit + blks_read = 0 THEN 1
                   ELSE blks_hit::float / (blks_hit + blks_read)
              END AS hit_ratio
            FROM pg_stat_database
            WHERE datname = current_database()
            """
        )
    )
    row = result.one()
    return {
        "blks_hit": int(row.blks_hit or 0),
        "blks_read": int(row.blks_read or 0),
        "hit_ratio": float(row.hit_ratio or 0.0),
    }


async def top_request_log_rows(
    session: AsyncSession, *, limit: int = 10
) -> list[TopRow]:
    """The ``limit`` largest ``request_log`` rows by combined body size.

    Uses the stored ``request_bytes`` / ``response_bytes`` columns
    (the **original** sizes, even after MAX_BODY_BYTES truncation),
    which is the right number for "who is generating the most data".
    """
    result = await session.execute(
        text(
            """
            SELECT rl.id                          AS log_id,
                   rl.request_id                  AS request_id,
                   rl.created_at                  AS created_at,
                   rl.endpoint                    AS endpoint,
                   u.email                        AS user_email,
                   rl.request_bytes               AS request_bytes,
                   rl.response_bytes              AS response_bytes,
                   COALESCE(rl.request_bytes, 0)
                     + COALESCE(rl.response_bytes, 0) AS total_bytes
            FROM request_log rl
            LEFT JOIN users u ON u.id = rl.user_id
            ORDER BY (COALESCE(rl.request_bytes, 0)
                      + COALESCE(rl.response_bytes, 0)) DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [
        TopRow(
            log_id=int(r.log_id),
            request_id=str(r.request_id),
            created_at=r.created_at,
            endpoint=r.endpoint,
            user_email=r.user_email,
            request_bytes=(int(r.request_bytes) if r.request_bytes is not None else None),
            response_bytes=(int(r.response_bytes) if r.response_bytes is not None else None),
            total_bytes=int(r.total_bytes or 0),
        )
        for r in result.all()
    ]


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


# Prefix → human-friendly label. Anything that doesn't match falls into
# the catch-all ``other`` bucket. Keep this list small — counting keys
# uses ``SCAN`` and walks the entire keyspace once per category.
_REDIS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ratelimit:*", "ratelimit"),
    ("refresh:*", "refresh_blocklist"),
    ("idem:*", "idempotency"),
)


async def redis_summary(client: redis_asyncio.Redis) -> dict[str, Any]:
    """Memory + key-count snapshot from Redis.

    ``INFO memory`` is one round-trip and gives ``used_memory`` plus
    ``maxmemory`` / fragmentation. Per-prefix counts use ``SCAN`` so
    we don't block the server on large keyspaces; we cap the cost at
    ``MATCH`` patterns we actually own.
    """
    info_mem = await client.info(section="memory")
    info_clients = await client.info(section="clients")
    info_stats = await client.info(section="stats")
    dbsize = await client.dbsize()

    # SCAN per prefix. iter() chunks via COUNT=1000 by default which
    # is fine for our keyspace sizes; if Redis ever balloons past
    # tens of millions of keys this becomes the slow part of the page.
    by_prefix: dict[str, int] = {}
    for pattern, label in _REDIS_PREFIXES:
        n = 0
        async for _ in client.scan_iter(match=pattern, count=1000):
            n += 1
        by_prefix[label] = n
    by_prefix["other"] = max(0, int(dbsize) - sum(by_prefix.values()))

    used = int(info_mem.get("used_memory", 0))
    maxmem = int(info_mem.get("maxmemory", 0))
    return {
        "used_memory": used,
        "used_memory_peak": int(info_mem.get("used_memory_peak", 0)),
        "maxmemory": maxmem,
        "fragmentation_ratio": float(info_mem.get("mem_fragmentation_ratio", 0.0)),
        "connected_clients": int(info_clients.get("connected_clients", 0)),
        "total_commands_processed": int(info_stats.get("total_commands_processed", 0)),
        "keyspace_hits": int(info_stats.get("keyspace_hits", 0)),
        "keyspace_misses": int(info_stats.get("keyspace_misses", 0)),
        "evicted_keys": int(info_stats.get("evicted_keys", 0)),
        "dbsize": int(dbsize),
        "keys_by_prefix": by_prefix,
    }


# ---------------------------------------------------------------------------
# Host disk
# ---------------------------------------------------------------------------


def _disk_usage_blocking(path: str) -> dict[str, int]:
    """``shutil.disk_usage`` on ``path`` — runs in a thread."""
    usage = shutil.disk_usage(path)
    return {
        "total": int(usage.total),
        "used": int(usage.used),
        "free": int(usage.free),
    }


async def host_disk_usage(path: str = "/") -> dict[str, Any]:
    """Disk usage for the container's root filesystem.

    On a Compose deploy the app container's ``/`` lives on the host's
    Docker storage driver, which on a single-VPS deploy is the same
    physical disk as ``pgdata`` and ``redisdata``. So this is a
    reasonable proxy for "is the VPS disk filling up".
    """
    result = await asyncio.to_thread(_disk_usage_blocking, path)
    total = result["total"]
    used = result["used"]
    pct = (used / total * 100.0) if total > 0 else 0.0
    return {**result, "path": path, "used_percent": round(pct, 1)}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def app_uptime(started_monotonic: float | None) -> dict[str, Any]:
    """Seconds since lifespan startup, plus a humanised string.

    ``started_monotonic`` is captured in the FastAPI lifespan as
    ``time.monotonic()``; using monotonic avoids drift if the host
    clock is adjusted while the app is running.
    """
    if started_monotonic is None:
        return {"seconds": 0, "human": "unknown"}
    seconds = int(time.monotonic() - started_monotonic)
    return {"seconds": seconds, "human": _format_seconds(seconds)}


def _format_seconds(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def format_bytes(n: int | None) -> str:
    """Human-friendly bytes: 12.3 MB, 1.7 GB, etc.

    Used by the template via a Jinja filter so call sites can stay
    simple. Returns ``"0 B"`` for None / 0.
    """
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} {units[-1]}"
