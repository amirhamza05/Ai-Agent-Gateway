"""Body-truncation helpers.

Per §8.1 of the plan, every row written to ``request_log`` must cap its
``request_body`` and ``response_body`` columns at ``MAX_BODY_BYTES`` so a
single chatty request can't fill the disk. The original byte-size is still
recorded in ``request_bytes`` / ``response_bytes`` so logs stay truthful
even when the payload was clipped.

Two flavours:

* :func:`truncate` — for a Python ``str`` (e.g. JSON-encoded request body).
* :func:`truncate_bytes` — for the raw ``bytes`` accumulator that the
  streaming generator in ``gateway.routes.messages`` tees into.

Both return ``(stored_text, original_size_in_bytes)``.

Decoding policy: ``errors="ignore"`` is intentional. UTF-8 sequences are
1–4 bytes, so a hard byte-cut may land mid-codepoint. ``ignore`` drops the
incomplete tail; ``replace`` would insert U+FFFD which TOAST-compresses
poorly and looks weird in pgAdmin. The truncation marker (``\\n…[truncated]``)
makes it obvious that the row was clipped.
"""

from __future__ import annotations

_TRUNCATION_MARKER = "\n…[truncated]"


def truncate(text: str, max_bytes: int) -> tuple[str, int]:
    """Cap a string at ``max_bytes`` UTF-8 bytes.

    Returns ``(stored_text, original_size)`` where ``original_size`` is
    the UTF-8 byte length BEFORE clipping — always store this in the
    ``*_bytes`` log column so the row remains truthful.
    """
    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        return raw[:max_bytes].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER, len(raw)
    return text, len(raw)


def truncate_bytes(buf: bytes, max_bytes: int) -> tuple[str, int]:
    """Cap a raw ``bytes`` accumulator at ``max_bytes``.

    Used by the streaming tee in ``/v1/messages``: the handler appends
    each upstream chunk to a bounded ``bytearray`` (already clipped at
    ``max_bytes`` while accumulating) and passes the accumulator here at
    the end of the stream.

    Returns ``(stored_text, len(buf))``. NOTE: because the caller bounds
    the bytearray during accumulation, ``len(buf)`` is the SIZE OF THE
    ACCUMULATOR, not the true upstream total. The streaming handler in
    ``gateway.routes.messages`` therefore tracks a separate per-chunk
    counter and writes that into ``request_log.response_bytes`` instead
    of trusting the value returned here.
    """
    if len(buf) > max_bytes:
        return buf[:max_bytes].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER, len(buf)
    return buf.decode("utf-8", errors="ignore"), len(buf)
