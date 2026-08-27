"""Tests for the GitHub PAT wiring.

Two surfaces to verify:
1. `_export_github_token` sets GH_TOKEN/GITHUB_TOKEN when
   TD_GITHUB_TOKEN is configured, and is a no-op when unset.
2. `_build_capabilities_block` includes the GitHub hint when the
   env vars are present and is empty otherwise.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from taskdeck_runner.crp_client import _build_capabilities_block
from taskdeck_runner.main import _export_github_token
from taskdeck_runner.settings import RunnerSettings


def _settings(**overrides: object) -> RunnerSettings:
    base: dict[str, object] = {
        "TD_CORE_WS_URL": "ws://test",
        "TD_RUNNER_TOKEN": "t",
        "TD_RUNNER_NAME": "n",
        "TD_MAX_PARALLEL": 1,
        "TD_WORK_DIR": "/tmp/td-test",
        "TD_CORE_HTTP_URL": "http://test",
    }
    base.update(overrides)
    return RunnerSettings.model_validate(base)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Each test starts with no GH_TOKEN/GITHUB_TOKEN in env."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield


def test_export_no_op_when_token_unset() -> None:
    s = _settings()
    # Mock subprocess.run so a missing `gh` doesn't pollute the test.
    with patch("subprocess.run") as mock_run:
        _export_github_token(s)
    assert "GH_TOKEN" not in os.environ
    assert "GITHUB_TOKEN" not in os.environ
    # gh auth setup-git must NOT run when no token configured —
    # otherwise we'd be touching git config for nothing.
    mock_run.assert_not_called()


def test_export_sets_both_env_vars_when_configured() -> None:
    s = _settings(TD_GITHUB_TOKEN="ghp_test123")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        _export_github_token(s)
    assert os.environ["GH_TOKEN"] == "ghp_test123"
    assert os.environ["GITHUB_TOKEN"] == "ghp_test123"
    # Setup-git must be called so `git push https://...` works
    # without per-command credential URLs.
    args, kwargs = mock_run.call_args
    assert args[0] == ["gh", "auth", "setup-git"]


def test_export_does_not_overwrite_preexisting(monkeypatch) -> None:
    """systemd unit / operator override must beat .env config."""
    monkeypatch.setenv("GH_TOKEN", "operator-override")
    monkeypatch.setenv("GITHUB_TOKEN", "operator-override")
    s = _settings(TD_GITHUB_TOKEN="from-env-file")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        _export_github_token(s)
    assert os.environ["GH_TOKEN"] == "operator-override"
    assert os.environ["GITHUB_TOKEN"] == "operator-override"


def test_export_survives_missing_gh_binary() -> None:
    """Logging warning is fine; raising would crash the runner."""
    s = _settings(TD_GITHUB_TOKEN="ghp_x")
    with patch("subprocess.run", side_effect=FileNotFoundError("no gh")):
        # Must not raise.
        _export_github_token(s)
    # Token should still be exported — gh-CLI is optional, raw API
    # via curl + GH_TOKEN still works for agents.
    assert os.environ["GH_TOKEN"] == "ghp_x"


def test_capabilities_block_empty_without_token() -> None:
    assert _build_capabilities_block() == ""


def test_capabilities_block_mentions_github_with_token(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    block = _build_capabilities_block()
    assert "<capabilities>" in block
    assert "GH_TOKEN" in block
    # Steers agents away from the read-only deploy key — that's the
    # actual user-pain-point being fixed.
    assert "deploy key" in block.lower()
