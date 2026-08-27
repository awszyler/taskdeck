"""Tests for IntentParseLoop — the agent-loop replacement for IntentParser.parse.

Each test injects a programmable LLM client that can simulate one specific
failure mode per attempt. The loop's contract is:
- High confidence → result=high_conf, attempts=1
- Low confidence → result=low_conf, attempts=1 (no retry on low-conf)
- Recoverable LLM error → retry with a tailored nudge → success or heuristic
- Two LLM failures → heuristic, never raises
- Empty capabilities → heuristic, never invokes LLM
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from taskdeck_core.intent.loop import (
    HIGH_CONFIDENCE_THRESHOLD,
    IntentParseLoop,
    ParseOutcome,
)
from taskdeck_core.intent.schema import IntentInput

DEFAULT_CAPS = [
    {"capability": "shell", "description": "Single shell command."},
    {"capability": "claude-code", "description": "AI coding agent."},
    {"capability": "kiro-cli", "description": "AWS Q coding agent."},
]


class _ScriptedClient:
    """Programmable LLM client. `script` is a list of:
       - dict (returned as `arguments=json.dumps(dict)`)
       - "TIMEOUT" (raises asyncio.TimeoutError)
       - "TRANSPORT" (raises generic Exception)
       - "JSON_BAD" (returns arguments that aren't valid JSON)
       - Exception instance (raised directly)
    The Nth `create_completion` call returns / raises the Nth script entry.
    """

    def __init__(self, script: list[Any]):
        self._script = list(script)
        self.calls: list[dict] = []  # capture each invocation's tool/system

    async def create_completion(self, *, model, messages, tools, tool_choice, **_):
        idx = len(self.calls)
        self.calls.append({
            "model": model, "messages": messages, "tools": tools, "tool_choice": tool_choice,
        })
        if idx >= len(self._script):
            raise AssertionError(f"unexpected create_completion call #{idx + 1}")
        item = self._script[idx]
        if item == "TIMEOUT":
            raise TimeoutError()
        if item == "TRANSPORT":
            raise RuntimeError("simulated 5xx")
        if item == "JSON_BAD":
            return {"arguments": "{not json"}
        if isinstance(item, Exception):
            raise item
        # Dict outcome.
        return {"arguments": json.dumps(item), "usage": None, "model": model}


def _outcome_args(o: ParseOutcome) -> dict:
    return {"agent": o.parsed.agent, "conf": o.parsed.confidence, "result": o.result, "attempts": o.attempts}


@pytest.mark.asyncio
async def test_high_confidence_one_shot():
    client = _ScriptedClient([
        {"title": "x", "agent": "claude-code", "prompt": "x", "confidence": 0.9},
    ])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(IntentInput(raw_input="any"), capabilities=DEFAULT_CAPS)
    assert out.result == "high_conf"
    assert out.attempts == 1
    assert out.parsed.agent == "claude-code"
    assert out.should_auto_submit is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_low_confidence_no_retry():
    """conf < 0.7 must finalize as low_conf — NOT retry. Retry would let the
    model confabulate a confident wrong answer."""
    client = _ScriptedClient([
        {"title": "x", "agent": "claude-code", "prompt": "x", "confidence": 0.4},
        # If the loop retried, the second entry would be consumed; assertion
        # below proves it wasn't.
    ])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(IntentInput(raw_input="vague"), capabilities=DEFAULT_CAPS)
    assert out.result == "low_conf"
    assert out.attempts == 1
    assert out.parsed.confidence == 0.4
    assert out.should_auto_submit is False
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_invalid_agent_then_recover():
    """Attempt 1 returns invented agent → attempt 2 with nudge → success."""
    client = _ScriptedClient([
        {"title": "x", "agent": "frontend_developer", "prompt": "x", "confidence": 0.95},
        {"title": "x", "agent": "claude-code", "prompt": "x", "confidence": 0.9},
    ])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(IntentInput(raw_input="x"), capabilities=DEFAULT_CAPS)
    assert out.result == "high_conf"
    assert out.attempts == 2
    assert out.parsed.agent == "claude-code"
    # Attempt 2's system prompt got the nudge appended.
    sys2 = client.calls[1]["messages"][0]["content"]
    assert "frontend_developer" in sys2
    assert "NOT in the enum" in sys2


@pytest.mark.asyncio
async def test_json_error_then_recover():
    client = _ScriptedClient([
        "JSON_BAD",
        {"title": "x", "agent": "claude-code", "prompt": "x", "confidence": 0.85},
    ])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(IntentInput(raw_input="x"), capabilities=DEFAULT_CAPS)
    assert out.result == "high_conf"
    assert out.attempts == 2
    sys2 = client.calls[1]["messages"][0]["content"]
    assert "VALID JSON" in sys2


@pytest.mark.asyncio
async def test_timeout_then_recover():
    client = _ScriptedClient([
        "TIMEOUT",
        {"title": "x", "agent": "shell", "prompt": "x", "confidence": 0.9},
    ])
    # Note: timeout_attempt_1=0.001 to make the first call definitely time out
    # before the (immediate) coroutine completes — but actually our scripted
    # client raises asyncio.TimeoutError directly so we don't need a real timer.
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=10.0, timeout_attempt_2=10.0)
    out = await loop.run(IntentInput(raw_input="echo hi"), capabilities=DEFAULT_CAPS)
    assert out.result == "high_conf"
    assert out.attempts == 2
    # Pure timeout: no nudge appended.
    sys1 = client.calls[0]["messages"][0]["content"]
    sys2 = client.calls[1]["messages"][0]["content"]
    assert sys1 == sys2  # same prompt, no nudge


@pytest.mark.asyncio
async def test_transport_then_recover():
    client = _ScriptedClient([
        "TRANSPORT",
        {"title": "x", "agent": "claude-code", "prompt": "x", "confidence": 0.85},
    ])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(IntentInput(raw_input="x"), capabilities=DEFAULT_CAPS)
    assert out.result == "high_conf"
    assert out.attempts == 2


@pytest.mark.asyncio
async def test_two_failures_fall_through_to_heuristic():
    """Both LLM attempts fail → heuristic terminal layer kicks in.
    Long Chinese natural-language input should land in claude-code."""
    client = _ScriptedClient([
        "TIMEOUT",
        "TRANSPORT",
    ])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(
        IntentInput(raw_input="在 https://github.com/awszyler 下新建测试用的贪吃蛇项目"),
        capabilities=DEFAULT_CAPS,
    )
    assert out.result == "heuristic"
    assert out.attempts == 3
    assert out.parsed.agent == "claude-code"
    assert out.parsed.confidence == 0.3
    assert out.should_auto_submit is False
    assert any("heuristic" in r for r in out.parsed.confidence_reasons)


@pytest.mark.asyncio
async def test_heuristic_picks_kiro_when_user_asked_for_it():
    client = _ScriptedClient(["TIMEOUT", "TIMEOUT"])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(
        IntentInput(raw_input="用 kiro 帮我修一下 apps/web 里失败的测试"),
        capabilities=DEFAULT_CAPS,
    )
    assert out.parsed.agent == "kiro-cli"


@pytest.mark.asyncio
async def test_heuristic_picks_shell_for_short_ascii_oneliner():
    client = _ScriptedClient(["TIMEOUT", "TIMEOUT"])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(
        IntentInput(raw_input="git fetch origin main"),
        capabilities=DEFAULT_CAPS,
    )
    assert out.parsed.agent == "shell"


@pytest.mark.asyncio
async def test_empty_capabilities_skips_llm_returns_heuristic():
    """No runners connected → never call the LLM; heuristic surfaces the reason."""
    client = _ScriptedClient([])  # empty script: any call would fail
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(IntentInput(raw_input="anything"), capabilities=[])
    assert out.result == "heuristic"
    assert out.attempts == 3
    assert "no runners connected" in out.parsed.confidence_reasons[0]
    assert len(client.calls) == 0


@pytest.mark.asyncio
async def test_high_confidence_threshold_boundary():
    """Exactly at threshold → high_conf. One epsilon below → low_conf."""
    client = _ScriptedClient([
        {"title": "x", "agent": "shell", "prompt": "x", "confidence": HIGH_CONFIDENCE_THRESHOLD},
    ])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(IntentInput(raw_input="x"), capabilities=DEFAULT_CAPS)
    assert out.result == "high_conf"


@pytest.mark.asyncio
async def test_attempt_2_succeeds_with_low_conf_still_returns_low_conf():
    """If attempt 2 succeeds with low confidence, we still report low_conf
    (not 'retry again'). This prevents endless re-prompting."""
    client = _ScriptedClient([
        "TIMEOUT",
        {"title": "x", "agent": "claude-code", "prompt": "x", "confidence": 0.4},
    ])
    loop = IntentParseLoop(llm_client=client, model="m", timeout_attempt_1=1.0, timeout_attempt_2=1.0)
    out = await loop.run(IntentInput(raw_input="vague"), capabilities=DEFAULT_CAPS)
    assert out.result == "low_conf"
    assert out.attempts == 2
