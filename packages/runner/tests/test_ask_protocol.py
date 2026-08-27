"""Tests for ccpt:ask extraction from agent stdout."""
from __future__ import annotations


def test_extracts_ask_from_clean_stdout():
    from taskdeck_runner.ask_protocol import extract_ask
    out = "Some output.\n<ccpt:ask>May I read README?</ccpt:ask>\n"
    assert extract_ask(out) == "May I read README?"


def test_takes_last_ask_when_multiple():
    from taskdeck_runner.ask_protocol import extract_ask
    out = (
        "<ccpt:ask>thinking out loud</ccpt:ask>\n"
        "more text\n"
        "<ccpt:ask>final actual question</ccpt:ask>"
    )
    assert extract_ask(out) == "final actual question"


def test_no_ask_returns_none():
    from taskdeck_runner.ask_protocol import extract_ask
    assert extract_ask("just regular output\n") is None
    assert extract_ask("") is None


def test_empty_ask_falls_through():
    from taskdeck_runner.ask_protocol import extract_ask
    assert extract_ask("<ccpt:ask></ccpt:ask>") is None
    assert extract_ask("<ccpt:ask>   </ccpt:ask>") is None


def test_multiline_ask_supported():
    from taskdeck_runner.ask_protocol import extract_ask
    out = "<ccpt:ask>line one\nline two\nline three</ccpt:ask>"
    assert extract_ask(out) == "line one\nline two\nline three"


def test_ask_with_surrounding_text():
    from taskdeck_runner.ask_protocol import extract_ask
    out = "I think I should ask:\n<ccpt:ask>can I run rm?</ccpt:ask>\nLet me know."
    assert extract_ask(out) == "can I run rm?"
