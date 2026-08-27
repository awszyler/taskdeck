from __future__ import annotations

from taskdeck_runner.crp_client import CRPClient
from taskdeck_runner.settings import RunnerSettings


def _settings(**overrides: object) -> RunnerSettings:
    # RunnerSettings uses field aliases (e.g. TD_CORE_WS_URL) as dict keys.
    defaults: dict[str, object] = {
        "TD_CORE_WS_URL": "ws://test",
        "TD_RUNNER_TOKEN": "t",
        "TD_RUNNER_NAME": "n",
        "TD_MAX_PARALLEL": 1,
        "TD_WORK_DIR": "/tmp/td-test",
        "TD_CORE_HTTP_URL": "http://test",
    }
    defaults.update(overrides)
    return RunnerSettings.model_validate(defaults)


def test_capabilities_shell_only_by_default() -> None:
    client = CRPClient(_settings())
    caps = client._build_capabilities()
    assert caps == ["shell"]


def test_capabilities_add_claude_code_when_configured() -> None:
    client = CRPClient(_settings(TD_CLAUDE_CODE_BIN="/usr/bin/claude"))
    assert "claude-code" in client._build_capabilities()


def test_capabilities_add_agentcore_when_enabled() -> None:
    client = CRPClient(_settings(
        TD_AGENTCORE_ENABLED=True,
        TD_AGENTCORE_AGENT_IDS="foo,bar",
    ))
    caps = client._build_capabilities()
    assert "agentcore-foo" in caps
    assert "agentcore-bar" in caps


def test_capabilities_skip_agentcore_when_disabled() -> None:
    client = CRPClient(_settings(
        TD_AGENTCORE_ENABLED=False,
        TD_AGENTCORE_AGENT_IDS="foo",
    ))
    assert "agentcore-foo" not in client._build_capabilities()


def test_capabilities_add_openclaw_when_configured() -> None:
    client = CRPClient(_settings(TD_OPENCLAW_BIN="/usr/bin/openclaw"))
    assert "openclaw" in client._build_capabilities()


def test_capabilities_add_hermes_when_configured() -> None:
    client = CRPClient(_settings(TD_HERMES_BIN="/usr/bin/hermes"))
    assert "hermes" in client._build_capabilities()


def test_capabilities_add_codex_when_configured() -> None:
    client = CRPClient(_settings(TD_CODEX_BIN="/usr/bin/codex"))
    assert "codex" in client._build_capabilities()
