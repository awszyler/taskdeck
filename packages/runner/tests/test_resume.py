"""Tests for resumed-prompt construction from prior turns."""
from __future__ import annotations


def _turn(role: str, content: str):
    """Lightweight turn for tests; matches PriorTurn fields by attribute."""
    from types import SimpleNamespace
    return SimpleNamespace(role=role, content=content)


def test_first_dispatch_no_prior_turns_returns_original():
    from taskdeck_runner.resume import build_resumed_prompt
    assert build_resumed_prompt("do the thing", []) == "do the thing"


def test_resumed_prompt_includes_history():
    from taskdeck_runner.resume import build_resumed_prompt
    turns = [
        _turn("agent", "May I read README.md?"),
        _turn("user", "yes please"),
    ]
    prompt = build_resumed_prompt("explain section 2", turns)
    assert "explain section 2" in prompt
    assert "You previously asked: May I read README.md?" in prompt
    assert "User answered: yes please" in prompt
    assert "<ccpt:ask>" in prompt  # tells the agent how to ask again
    # Original task appears before history.
    assert prompt.index("explain section 2") < prompt.index("You previously asked")


def test_resumed_prompt_with_only_agent_turn():
    """Edge case: agent asked but user hasn't answered yet (shouldn't happen
    in practice — we only re-dispatch after user replies — but defensive)."""
    from taskdeck_runner.resume import build_resumed_prompt
    turns = [_turn("agent", "ping?")]
    prompt = build_resumed_prompt("original", turns)
    assert "You previously asked: ping?" in prompt


def test_resumed_prompt_alternation():
    """Three rounds: agent, user, agent, user."""
    from taskdeck_runner.resume import build_resumed_prompt
    turns = [
        _turn("agent", "Q1?"),
        _turn("user", "A1"),
        _turn("agent", "Q2?"),
        _turn("user", "A2"),
    ]
    prompt = build_resumed_prompt("original", turns)
    p = prompt
    i_q1 = p.index("Q1?")
    i_a1 = p.index("A1")
    i_q2 = p.index("Q2?")
    i_a2 = p.index("A2")
    assert i_q1 < i_a1 < i_q2 < i_a2
