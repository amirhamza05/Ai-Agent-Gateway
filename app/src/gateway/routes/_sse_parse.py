"""Tiny SSE / Anthropic-event parsing for token-usage extraction.

Used only by ``/v1/messages`` to read ``input_tokens`` / ``output_tokens``
out of the upstream stream so the gateway can compute and log cost.

Why scan the raw text rather than a real SSE parser:

* The streaming handler MUST yield bytes to the client as fast as they
  arrive. A line-buffered SSE parser would have to coalesce partial
  events before exposing them, which adds first-token latency.
* The gateway doesn't act on the events — it just needs the final usage
  numbers. So it tees a bounded byte accumulator and runs a trailing
  regex over its tail at end-of-stream (and incrementally per chunk so
  the search window stays small).
* Anthropic's SSE shape is stable enough that a regex is fine.
  ``message_delta`` carries ``"usage": {"input_tokens": N, "output_tokens": M}``
  near the end of the stream; we keep the LAST occurrence we find because
  Anthropic emits cumulative usage across deltas.

If the regex finds nothing, the caller logs ``messages.usage_not_found``
once and stores ``tokens_in=tokens_out=0``.
"""

from __future__ import annotations

import json
import re

# Match ``"usage" : { ... }`` non-greedily. ``[^}]*`` is fine because the
# Anthropic ``usage`` object is flat (no nested objects) — if that ever
# changes upstream, swap to a real JSON-fragment scanner.
_USAGE_RE = re.compile(r'"usage"\s*:\s*(\{[^}]*\})')


def extract_usage(buffer: str) -> tuple[int | None, int | None]:
    """Return ``(input_tokens, output_tokens)`` from an SSE text buffer.

    Thin wrapper around :func:`extract_usage_full` for callers that only
    need the regular input/output counts. New code that also needs cache
    counts should call :func:`extract_usage_full` directly.
    """
    in_t, out_t, _, _ = extract_usage_full(buffer)
    return in_t, out_t


def extract_usage_full(
    buffer: str,
) -> tuple[int | None, int | None, int | None, int | None]:
    """Return ``(input, output, cache_read, cache_write)`` from a buffer.

    Scans for the LAST ``"usage": {...}`` JSON fragment in ``buffer`` and
    parses it. Returns ``(None, None, None, None)`` if no usage object is
    found OR if the matched fragment fails to parse as JSON (defensive —
    a partial chunk could yield a malformed match while bytes are still
    in flight).

    Anthropic reports cache tokens under the keys
    ``cache_read_input_tokens`` (cache hits, billed at the cheap rate)
    and ``cache_creation_input_tokens`` (tokens written into the cache,
    billed at the premium rate). They are reported alongside
    ``input_tokens``/``output_tokens`` and are NOT counted inside the
    ``input_tokens`` figure — see the Anthropic streaming usage docs.

    Returned values are ``int | None`` per field; the call site coerces
    missing entries to 0 for storage.
    """
    matches = _USAGE_RE.findall(buffer)
    if not matches:
        return None, None, None, None

    # Walk matches from newest to oldest so a malformed trailing match
    # (e.g. a chunk boundary clipped mid-object) falls back to the last
    # complete one.
    for raw in reversed(matches):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        in_t = obj.get("input_tokens")
        out_t = obj.get("output_tokens")
        cache_read = obj.get("cache_read_input_tokens")
        cache_write = obj.get("cache_creation_input_tokens")
        # Only return if at least one of the four showed up — a stray
        # ``"usage": {}`` in a different context shouldn't override a
        # real reading from earlier in the stream.
        if (
            in_t is not None
            or out_t is not None
            or cache_read is not None
            or cache_write is not None
        ):
            return (
                int(in_t) if in_t is not None else None,
                int(out_t) if out_t is not None else None,
                int(cache_read) if cache_read is not None else None,
                int(cache_write) if cache_write is not None else None,
            )
    return None, None, None, None
