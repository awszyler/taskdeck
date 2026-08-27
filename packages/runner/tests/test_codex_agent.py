"""Tests for CodexExecutor.

Drive a fake `codex` shim that emits a representative JSONL transcript.
Asserts:
- agent_message text is forwarded to stdout
- command_execution items render as one-line tool hints
- the LAST agent_message becomes the summary
- the `-o` last-message file is read as a fallback when JSONL omits it
- exit codes propagate
- argv carries the flags codex needs to run headless
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from taskdeck_runner.agents.codex import CodexExecutor

if TYPE_CHECKING:
    from pathlib import Path


def _make_shim(tmp_path: Path, *, exit_code: int, jsonl_events: list[dict]) -> Path:
    """Create a fake codex binary that prints JSONL events on stdout
    then exits. We embed events as a heredoc — keeps the test
    hermetic (no real codex required)."""
    body = "\n".join(json.dumps(ev) for ev in jsonl_events)
    shim = tmp_path / "fake-codex"
    shim.write_text(
        f"""#!/bin/sh
# Echo argv so tests can assert flag passing.
echo "argv:$*" 1>&2
cat <<'EOF'
{body}
EOF
exit {exit_code}
"""
    )
    shim.chmod(0o755)
    return shim


@pytest.mark.asyncio
async def test_renders_agent_messages_and_tool_calls(tmp_path: Path) -> None:
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        # First agent narrative.
        {"type": "item.completed", "item": {
            "id": "i0", "type": "agent_message",
            "text": "I will list the directory now.",
        }},
        # Bash tool call: only the completed event should render.
        {"type": "item.started", "item": {
            "id": "i1", "type": "command_execution",
            "command": "/bin/bash -lc ls", "exit_code": None, "status": "in_progress",
        }},
        {"type": "item.completed", "item": {
            "id": "i1", "type": "command_execution",
            "command": "/bin/bash -lc ls", "exit_code": 0, "status": "completed",
        }},
        # Final agent narrative becomes the summary.
        {"type": "item.completed", "item": {
            "id": "i2", "type": "agent_message",
            "text": "The directory is empty. Done.",
        }},
        {"type": "turn.completed"},
    ]
    shim = _make_shim(tmp_path, exit_code=0, jsonl_events=events)

    exec_ = CodexExecutor(bin_path=str(shim))
    out = [e async for e in exec_.run(task_id="t", prompt="ls", cwd=tmp_path)]

    stdouts = [data for kind, data in out if kind == "stdout"]
    finish = [e for e in out if e[0] == "finish"]

    assert finish == [("finish", "0")]
    # Both agent messages are surfaced verbatim.
    assert any("list the directory" in s for s in stdouts)
    assert any("directory is empty" in s for s in stdouts)
    # Tool call rendered as a single hint (only one — completed, not started).
    bash_lines = [s for s in stdouts if "[tool: bash]" in s]
    assert len(bash_lines) == 1
    assert "/bin/bash -lc ls" in bash_lines[0]
    # Summary is the LAST agent_message.
    assert exec_.summary() == "The directory is empty. Done."


@pytest.mark.asyncio
async def test_argv_carries_required_flags(tmp_path: Path) -> None:
    shim = _make_shim(tmp_path, exit_code=0, jsonl_events=[])
    exec_ = CodexExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t", prompt="hello", cwd=tmp_path)]

    # Shim echoes argv on stderr.
    stderr = "\n".join(d for k, d in events if k == "stderr")
    assert "exec" in stderr
    assert "--json" in stderr
    assert "--skip-git-repo-check" in stderr
    assert "--dangerously-bypass-approvals-and-sandbox" in stderr
    assert "--cd" in stderr
    assert "-o" in stderr
    assert "hello" in stderr  # the prompt we passed


@pytest.mark.asyncio
async def test_falls_back_to_last_message_file_when_no_jsonl_summary(
    tmp_path: Path,
) -> None:
    """Even if codex emits no agent_message in JSONL (mid-turn exit,
    custom output schema, ...) the `-o` file is the source of truth."""
    # Shim only writes the last-message file, then exits 0.
    shim = tmp_path / "fake-codex"
    shim.write_text(
        """#!/bin/sh
# Find the -o argument and write to it.
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    printf 'summary-from-file' > "$2"
    break
  fi
  shift
done
exit 0
"""
    )
    shim.chmod(0o755)

    exec_ = CodexExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t", prompt="x", cwd=tmp_path)]

    finish = [e for e in events if e[0] == "finish"]
    assert finish == [("finish", "0")]
    assert exec_.summary() == "summary-from-file"


@pytest.mark.asyncio
async def test_nonzero_exit_propagates(tmp_path: Path) -> None:
    shim = _make_shim(tmp_path, exit_code=2, jsonl_events=[])
    exec_ = CodexExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t", prompt="x", cwd=tmp_path)]
    finish = [e for e in events if e[0] == "finish"]
    assert finish == [("finish", "2")]


def test_rejects_empty_bin() -> None:
    with pytest.raises(ValueError):
        CodexExecutor(bin_path="")
