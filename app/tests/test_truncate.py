"""Unit tests for ``gateway.truncate``.

Pure-function tests — no DB, no event loop, no app fixtures required.
"""

from __future__ import annotations

from gateway.truncate import truncate, truncate_bytes


def test_truncate_passthrough_when_under_limit() -> None:
    text = "hello world"
    stored, original = truncate(text, max_bytes=1024)
    assert stored == text
    assert original == len(text.encode("utf-8"))


def test_truncate_clips_when_over_limit() -> None:
    payload = "A" * 100
    stored, original = truncate(payload, max_bytes=20)
    assert stored.startswith("A" * 20)
    assert "[truncated]" in stored
    assert original == 100


def test_truncate_handles_multibyte_codepoint_safely() -> None:
    """A hard byte-cut that lands mid-codepoint must not crash."""
    # "é" is two bytes in UTF-8. Cutting in the middle of one would
    # produce an invalid sequence; ``errors="ignore"`` discards the
    # incomplete tail.
    payload = "é" * 100  # 200 bytes
    stored, original = truncate(payload, max_bytes=21)  # odd → mid-codepoint
    assert original == 200
    assert stored.endswith("[truncated]")
    # Stored prefix decodes cleanly.
    stored_prefix = stored.replace("\n…[truncated]", "")
    assert all(c == "é" for c in stored_prefix)


def test_truncate_bytes_passthrough_when_under_limit() -> None:
    buf = b"hello world"
    stored, size = truncate_bytes(buf, max_bytes=1024)
    assert stored == "hello world"
    assert size == 11


def test_truncate_bytes_clips_when_over_limit() -> None:
    buf = b"X" * 100
    stored, size = truncate_bytes(buf, max_bytes=20)
    assert "[truncated]" in stored
    assert stored.startswith("X" * 20)
    assert size == 100


def test_truncate_bytes_handles_invalid_utf8() -> None:
    """Raw bytes from a stream may include invalid UTF-8 at chunk boundaries."""
    buf = b"\xff\xfeabc"  # nonsense BOM-like prefix
    stored, size = truncate_bytes(buf, max_bytes=1024)
    # ``errors="ignore"`` drops the invalid prefix; ``abc`` survives.
    assert "abc" in stored
    assert size == len(buf)
