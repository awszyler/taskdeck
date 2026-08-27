"""Test capability_descriptions builder."""
from __future__ import annotations

from taskdeck_runner.capability_descriptions import build_descriptions


class _FakeSettings:
    def __init__(self, **kw):
        self.claude_code_bin = kw.get("claude_code_bin")
        self.kiro_cli_bin = kw.get("kiro_cli_bin")
        self.openclaw_bin = kw.get("openclaw_bin")
        self.hermes_bin = kw.get("hermes_bin")
        self.codex_bin = kw.get("codex_bin")
        self.agentcore_enabled = kw.get("agentcore_enabled", False)
        self.agentcore_agent_ids = kw.get("agentcore_agent_ids", [])
        self.agentcore_agent_descriptions = kw.get("agentcore_agent_descriptions")


def test_shell_only_minimal():
    out = build_descriptions(_FakeSettings())  # type: ignore[arg-type]
    assert set(out.keys()) == {"shell"}
    assert "shell command" in out["shell"].lower()


def test_full_runtime_set():
    s = _FakeSettings(
        claude_code_bin="/x/claude",
        kiro_cli_bin="/x/kiro-cli",
        agentcore_enabled=True,
        agentcore_agent_ids=["AB12CD34", "FAKE0001"],
        agentcore_agent_descriptions='{"AB12CD34": "Deploys static sites to S3 + CloudFront."}',
    )
    out = build_descriptions(s)  # type: ignore[arg-type]
    assert "claude-code" in out
    assert "kiro-cli" in out
    assert out["agentcore-AB12CD34"] == "Deploys static sites to S3 + CloudFront."
    # Missing description falls back to a neutral placeholder.
    assert "no description configured" in out["agentcore-FAKE0001"]


def test_codex_description_when_configured():
    s = _FakeSettings(codex_bin="/x/codex")
    out = build_descriptions(s)  # type: ignore[arg-type]
    assert "codex" in out
    assert "codex" in out["codex"].lower()


def test_invalid_json_does_not_crash():
    s = _FakeSettings(
        agentcore_enabled=True,
        agentcore_agent_ids=["X"],
        agentcore_agent_descriptions="not-json{{",
    )
    out = build_descriptions(s)  # type: ignore[arg-type]
    # Falls back to placeholder when JSON parse fails.
    assert "no description configured" in out["agentcore-X"]
