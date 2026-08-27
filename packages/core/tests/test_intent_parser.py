from __future__ import annotations

import asyncio
import json

from taskdeck_core.intent.parser import IntentParser
from taskdeck_core.intent.schema import IntentInput, ParsedIntent

_DEFAULT_CAPS = [
    {"capability": "shell", "description": "Single shell command."},
    {"capability": "claude-code", "description": "AI coding agent."},
]


class _FakeClient:
    def __init__(self, payload: dict | None = None, *, raise_exc: Exception | None = None, delay: float = 0):
        self._payload = payload
        self._raise = raise_exc
        self._delay = delay

    async def create_completion(self, **_) -> dict:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise:
            raise self._raise
        return {"arguments": json.dumps(self._payload)}


async def test_happy_path_parses_fully():
    fake = _FakeClient({
        "title": "Add validation to login page",
        "agent": "claude-code",
        "repo": "github.com/me/multica",
        "base_branch": "main",
        "priority": "normal",
        "prompt": "Add form validation to the login page.",
        "confidence": 0.92,
        "confidence_reasons": ["clear action", "matched repo"],
    })
    parser = IntentParser(client=fake, model="m", timeout=3.0)
    result = await parser.parse(
        IntentInput(raw_input="add validation to the login page"),
        available_capabilities=_DEFAULT_CAPS,
    )
    assert isinstance(result, ParsedIntent)
    assert result.agent == "claude-code"
    assert result.confidence == 0.92
    assert "matched repo" in result.confidence_reasons


async def test_timeout_returns_fallback():
    fake = _FakeClient({}, delay=5.0)
    parser = IntentParser(client=fake, model="m", timeout=0.1)
    result = await parser.parse(
        IntentInput(raw_input="do something"),
        available_capabilities=_DEFAULT_CAPS,
    )
    assert result.confidence == 0.0
    assert result.title == "do something"
    assert result.agent == "shell"


async def test_client_error_returns_fallback():
    fake = _FakeClient({}, raise_exc=RuntimeError("503 from LLM"))
    parser = IntentParser(client=fake, model="m", timeout=3.0)
    result = await parser.parse(
        IntentInput(raw_input="help me"),
        available_capabilities=_DEFAULT_CAPS,
    )
    assert result.confidence == 0.0
    assert "503" in result.confidence_reasons[0]


async def test_schema_mismatch_returns_fallback():
    # Missing required "confidence" field.
    fake = _FakeClient({"title": "x", "agent": "shell", "prompt": "x"})
    parser = IntentParser(client=fake, model="m", timeout=3.0)
    result = await parser.parse(
        IntentInput(raw_input="do x"),
        available_capabilities=_DEFAULT_CAPS,
    )
    assert result.confidence == 0.0


async def test_no_capabilities_returns_zero_confidence_without_calling_llm():
    """If no runners are connected, parser must NOT invoke the LLM and should
    surface a clear reason."""

    class _ExplodingClient:
        async def create_completion(self, **_) -> dict:
            raise AssertionError("LLM must not be called when no runners are connected")

    parser = IntentParser(client=_ExplodingClient(), model="m", timeout=3.0)
    result = await parser.parse(IntentInput(raw_input="anything"), available_capabilities=[])
    assert result.confidence == 0.0
    assert "no runners connected" in result.confidence_reasons[0]


async def test_invented_agent_value_is_downgraded():
    """If the LLM hallucinates an agent value not in the enum, parser must
    downgrade rather than let an undeliverable agent leak through."""
    fake = _FakeClient({
        "title": "x",
        "agent": "frontend_developer",  # not in caps enum
        "prompt": "x",
        "confidence": 0.95,
    })
    parser = IntentParser(client=fake, model="m", timeout=3.0)
    result = await parser.parse(
        IntentInput(raw_input="anything"),
        available_capabilities=_DEFAULT_CAPS,
    )
    assert result.agent == "shell"
    assert result.confidence == 0.0
    assert any("unknown agent" in r for r in result.confidence_reasons)


async def test_fallback_prefers_claude_code_for_natural_language_input():
    """When the LLM times out on a clearly non-shell prompt (CJK, long, etc.),
    fallback should NOT be shell — that would mis-execute the input as a bash
    command. claude-code is the right default."""
    fake = _FakeClient({}, raise_exc=TimeoutError(""))
    parser = IntentParser(client=fake, model="m", timeout=3.0)
    result = await parser.parse(
        IntentInput(raw_input="在 https://github.com/awszyler 下新建测试用的贪吃蛇项目"),
        available_capabilities=_DEFAULT_CAPS,
    )
    assert result.agent == "claude-code"
    assert result.confidence == 0.0
    assert "parser unavailable" in result.confidence_reasons[0]


async def test_fallback_keeps_shell_for_ascii_short_input():
    """Bare ASCII single-line input still falls back to shell — that's the
    only safe interpretation when the LLM is down."""
    fake = _FakeClient({}, raise_exc=TimeoutError(""))
    parser = IntentParser(client=fake, model="m", timeout=3.0)
    result = await parser.parse(
        IntentInput(raw_input="echo hi"),
        available_capabilities=_DEFAULT_CAPS,
    )
    assert result.agent == "shell"


async def test_fallback_uses_shell_when_claude_code_not_available():
    """If claude-code isn't a connected runtime, fallback must use shell
    even for natural-language input — picking an offline agent would just
    park the task forever."""
    fake = _FakeClient({}, raise_exc=TimeoutError(""))
    parser = IntentParser(client=fake, model="m", timeout=3.0)
    caps_no_claude = [{"capability": "shell", "description": "S"}]
    result = await parser.parse(
        IntentInput(raw_input="帮我写一个贪吃蛇游戏并部署到 S3+CloudFront"),
        available_capabilities=caps_no_claude,
    )
    assert result.agent == "shell"


async def test_dynamic_enum_includes_kiro_cli_when_present():
    """Tool schema's enum reflects current capabilities, not hard-coded set."""
    seen_tool: dict = {}

    class _Sniffer:
        async def create_completion(self, *, tools, **_) -> dict:
            seen_tool["tools"] = tools
            return {"arguments": json.dumps({
                "title": "x", "agent": "kiro-cli", "prompt": "x", "confidence": 0.9,
            })}

    parser = IntentParser(client=_Sniffer(), model="m", timeout=3.0)
    caps = [
        {"capability": "shell", "description": "S"},
        {"capability": "claude-code", "description": "CC"},
        {"capability": "kiro-cli", "description": "KC"},
    ]
    await parser.parse(IntentInput(raw_input="use kiro to do x"), available_capabilities=caps)
    enum_values = seen_tool["tools"][0]["function"]["parameters"]["properties"]["agent"]["enum"]
    assert enum_values == ["shell", "claude-code", "kiro-cli"]
