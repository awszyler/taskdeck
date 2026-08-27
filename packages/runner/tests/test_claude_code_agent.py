from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from taskdeck_runner.agents.claude_code import ClaudeCodeExecutor

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_executor_forwards_stdout_and_exit_zero(tmp_path: Path):
    # Shim records its argv and echoes the last argument (the prompt).
    shim = tmp_path / "fake-claude"
    shim.write_text(
        '#!/bin/sh\necho "argv:$*"\neval "echo prompt:\\${$#}"\nexit 0\n'
    )
    shim.chmod(0o755)

    exec_ = ClaudeCodeExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-1", prompt="echo-test")]
    streams = [e for e in events if e[0] in {"stdout", "stderr"}]
    finish = [e for e in events if e[0] == "finish"]

    stdout_text = "\n".join(data for _, data in streams)
    assert "prompt:echo-test" in stdout_text
    # Headless runner must run with permission checks bypassed; otherwise
    # tool calls (Write/Bash) block on an IDE prompt that nobody can answer.
    # See runbook §16.
    assert "--permission-mode" in stdout_text
    assert "bypassPermissions" in stdout_text
    assert finish == [("finish", "0")]


@pytest.mark.asyncio
async def test_executor_reports_nonzero_exit(tmp_path: Path):
    shim = tmp_path / "fake-claude"
    shim.write_text('#!/bin/sh\nexit 7\n')
    shim.chmod(0o755)

    exec_ = ClaudeCodeExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-2", prompt="x")]
    finish = [e for e in events if e[0] == "finish"]
    assert finish == [("finish", "7")]


def test_executor_rejects_empty_bin():
    with pytest.raises(ValueError):
        ClaudeCodeExecutor(bin_path="")
