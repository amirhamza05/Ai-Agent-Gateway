"""Unit tests for the trailing ``"usage"`` extractor used by /v1/messages."""

from __future__ import annotations

from gateway.routes._sse_parse import extract_usage


def test_extract_usage_basic_anthropic_event() -> None:
    sample = (
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{},"usage":{"input_tokens":11,"output_tokens":5}}\n\n'
    )
    assert extract_usage(sample) == (11, 5)


def test_extract_usage_picks_last_occurrence() -> None:
    """When multiple usage events appear, return the LAST (cumulative) one."""
    sample = (
        '"usage":{"input_tokens":10,"output_tokens":2}\n'
        'noise\n'
        '"usage":{"input_tokens":42,"output_tokens":17}\n'
    )
    assert extract_usage(sample) == (42, 17)


def test_extract_usage_returns_none_when_absent() -> None:
    sample = 'event: message_start\ndata: {"type":"message_start"}\n\n'
    assert extract_usage(sample) == (None, None)


def test_extract_usage_handles_missing_field_gracefully() -> None:
    """A partial ``usage`` object (only one of the two fields) is OK."""
    sample = '"usage":{"output_tokens":7}'
    assert extract_usage(sample) == (None, 7)


def test_extract_usage_skips_malformed_trailing_match() -> None:
    """A clipped trailing match falls back to the previous complete one."""
    sample = (
        '"usage":{"input_tokens":10,"output_tokens":3}\n'
        # Trailing fragment looks like usage but isn't valid JSON: must
        # not blow up the parser, must fall back to the earlier match.
        '"usage":{"input_tokens":99,'
    )
    # The malformed object lacks a closing brace inside the [^}]* match
    # window, so the regex captures up to the last ``}`` on the previous
    # line — which IS the complete first usage event. Verify we read 10/3.
    in_t, out_t = extract_usage(sample)
    assert in_t == 10
    assert out_t == 3


def test_extract_usage_empty_input() -> None:
    assert extract_usage("") == (None, None)
