"""Tests for the runner's summary derivation logic.

The CRP client uses executor.summary() if available; otherwise falls
back to the last 500 characters of joined stdout. This file directly
exercises the small helper logic without a full WS mock.
"""
from __future__ import annotations

from collections import deque


def _build_summary(stdout_tail: deque[str], executor_summary: str | None) -> str | None:
    """Mirrors crp_client.py's summary derivation. If you refactor the
    real implementation into a helper, replace this with an import.
    """
    if executor_summary:
        return executor_summary
    joined = "".join(stdout_tail).strip()
    return joined[-500:] if joined else None


def test_summary_uses_executor_override():
    tail = deque(["ignored stdout\n"])
    assert _build_summary(tail, "actual answer") == "actual answer"


def test_summary_falls_back_to_stdout_tail():
    tail = deque(["line one\n", "line two\n", "final line\n"])
    assert _build_summary(tail, None) == "line one\nline two\nfinal line"


def test_summary_truncates_to_500_chars():
    big = "x" * 600
    tail = deque([big])
    summary = _build_summary(tail, None)
    assert summary is not None
    assert len(summary) == 500
    assert summary == "x" * 500


def test_summary_none_when_empty():
    assert _build_summary(deque(), None) is None
    assert _build_summary(deque([""]), None) is None
    assert _build_summary(deque(["\n  \n"]), None) is None
