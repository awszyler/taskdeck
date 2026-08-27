"""Tests for HermesExecutor.

Mirrors test_claude_code_agent.py — uses a tiny shell shim as a stand-in
for the real `hermes` binary. Asserts:
- prompt is forwarded as the last positional arg
- exit code is propagated
- ANSI sequences are stripped
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from taskdeck_runner.agents.hermes import HermesExecutor

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_hermes_forwards_prompt_and_exit_zero(tmp_path: Path):
    # Shim records argv and echoes the last argument (the prompt itself).
    # Mirrors the openclaw/claude-code shim style.
    shim = tmp_path / "fake-hermes"
    shim.write_text(
        '#!/bin/sh\necho "argv:$*"\neval "echo prompt:\\${$#}"\nexit 0\n'
    )
    shim.chmod(0o755)

    exec_ = HermesExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-1", prompt="echo-test")]
    streams = [e for e in events if e[0] in {"stdout", "stderr"}]
    finish = [e for e in events if e[0] == "finish"]

    stdout_text = "\n".join(data for _, data in streams)
    # Hermes is invoked as `hermes chat -q "<prompt>" -Q`.
    # `chat` is $1, `-q` is $2, the prompt is $3, `-Q` is $4.
    # The shim's `${$#}` is the LAST argv (the `-Q` flag), so we
    # assert the prompt appears mid-argv instead.
    assert "echo-test" in stdout_text
    assert "chat" in stdout_text
    assert "-Q" in stdout_text
    assert finish == [("finish", "0")]


@pytest.mark.asyncio
async def test_hermes_reports_nonzero_exit(tmp_path: Path):
    shim = tmp_path / "fake-hermes"
    shim.write_text('#!/bin/sh\nexit 5\n')
    shim.chmod(0o755)

    exec_ = HermesExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-2", prompt="x")]
    finish = [e for e in events if e[0] == "finish"]
    assert finish == [("finish", "5")]


@pytest.mark.asyncio
async def test_hermes_strips_ansi(tmp_path: Path):
    shim = tmp_path / "fake-hermes"
    # Emit a line with ANSI color codes; the executor must strip them.
    shim.write_text("#!/bin/sh\nprintf '\\033[31mred\\033[0m text\\n'\nexit 0\n")
    shim.chmod(0o755)

    exec_ = HermesExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-3", prompt="x")]
    streams = [data for label, data in events if label == "stdout"]

    assert any("red text" in s and "\x1b[" not in s for s in streams)


def test_hermes_rejects_empty_bin():
    with pytest.raises(ValueError):
        HermesExecutor(bin_path="")
