"""Tests for OpenclawExecutor.

OpenClaw quirks vs other agents:
- Final JSON result goes to stderr, not stdout.
- Executor must extract the trailing JSON document and emit
  payloads[0].text as a synthesized stdout line.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from taskdeck_runner.agents.openclaw import (
    OpenclawExecutor,
    _extract_final_text,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_extract_final_text_happy_path():
    stderr = (
        "INFO log line 1\n"
        "INFO log line 2\n"
        "{\n"
        '  "payloads": [{"text": "Hello World", "mediaUrl": null}],\n'
        '  "meta": {"stopReason": "stop"}\n'
        "}\n"
    )
    assert _extract_final_text(stderr) == "Hello World"


def test_extract_final_text_no_json_returns_none():
    assert _extract_final_text("just some logs\nno json here\n") is None


def test_extract_final_text_empty_payloads_returns_none():
    stderr = '{\n"payloads": [],\n"meta": {}\n}\n'
    assert _extract_final_text(stderr) is None


def test_extract_final_text_malformed_json_returns_none():
    # Unbalanced braces — should be caught by the JSON parser.
    assert _extract_final_text("{\nnot valid json") is None


def test_extract_final_text_payload_without_text_field():
    stderr = '{\n"payloads": [{"mediaUrl": "x"}],\n"meta": {}\n}\n'
    assert _extract_final_text(stderr) is None


@pytest.mark.asyncio
async def test_openclaw_synthesizes_final_text_to_stdout(tmp_path: Path):
    # Shim emits JSON to stderr (matching real openclaw behavior) and
    # nothing to stdout. The executor should detect payloads[0].text and
    # emit it as a synthetic stdout line.
    shim = tmp_path / "fake-openclaw"
    json_payload = json.dumps({
        "payloads": [{"text": "synthesized reply", "mediaUrl": None}],
        "meta": {"stopReason": "stop"},
    }, indent=2)
    # Use printf so the JSON's literal { stays at start of line.
    shim.write_text(
        "#!/bin/sh\n"
        "printf 'INFO bootstrap line\\n' >&2\n"
        f"printf '%s\\n' '{json_payload}' >&2\n"
        "exit 0\n"
    )
    shim.chmod(0o755)

    exec_ = OpenclawExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-1", prompt="hi")]
    stdout_lines = [data for label, data in events if label == "stdout"]
    finish = [e for e in events if e[0] == "finish"]

    assert "synthesized reply" in "\n".join(stdout_lines)
    assert finish == [("finish", "0")]


@pytest.mark.asyncio
async def test_openclaw_passes_agent_name(tmp_path: Path):
    # Use a non-zero exit so the executor's failure-tail dump emits
    # the buffered stderr — that's our window into what argv was used.
    # On success, stderr is intentionally swallowed (see executor docs).
    shim = tmp_path / "fake-openclaw"
    shim.write_text("#!/bin/sh\necho \"argv:$*\" >&2\nexit 1\n")
    shim.chmod(0o755)

    exec_ = OpenclawExecutor(bin_path=str(shim), agent_name="my-agent")
    events = [e async for e in exec_.run(task_id="t-2", prompt="x")]
    stderr_lines = [data for label, data in events if label == "stderr"]

    joined = "\n".join(stderr_lines)
    assert "my-agent" in joined
    assert "agent" in joined  # the `agent` subcommand
    assert "--local" in joined


@pytest.mark.asyncio
async def test_openclaw_swallows_stderr_on_success(tmp_path: Path):
    """Successful runs must NOT emit stderr to the runner — it's
    buffered locally only, since openclaw's stderr is dominated by
    a verbose JSON system-prompt report users shouldn't see."""
    shim = tmp_path / "fake-openclaw"
    shim.write_text(
        "#!/bin/sh\n"
        # Lots of fake INFO lines, all to stderr.
        "for i in 1 2 3 4 5; do echo \"INFO line $i\" >&2; done\n"
        # Then a fake JSON tail with a final text.
        'printf "{\\n\\"payloads\\": [{\\"text\\": \\"ok reply\\"}],\\n\\"meta\\": {}\\n}\\n" >&2\n'
        "exit 0\n"
    )
    shim.chmod(0o755)

    exec_ = OpenclawExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-x", prompt="hi")]
    stderr_lines = [data for label, data in events if label == "stderr"]
    stdout_lines = [data for label, data in events if label == "stdout"]

    # Zero stderr passthrough on success — that's the whole point.
    assert stderr_lines == []
    # Synthesized text still reaches stdout for summary capture.
    assert any("ok reply" in s for s in stdout_lines)


@pytest.mark.asyncio
async def test_openclaw_dumps_stderr_tail_on_failure(tmp_path: Path):
    shim = tmp_path / "fake-openclaw"
    shim.write_text(
        "#!/bin/sh\n"
        "echo 'plugin loaded' >&2\n"
        "echo 'ERROR: something exploded' >&2\n"
        "exit 2\n"
    )
    shim.chmod(0o755)

    exec_ = OpenclawExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-fail", prompt="x")]
    stderr_lines = [data for label, data in events if label == "stderr"]

    # On failure, the tail is dumped so the user can postmortem.
    joined = "\n".join(stderr_lines)
    assert "ERROR: something exploded" in joined


@pytest.mark.asyncio
async def test_openclaw_reports_nonzero_exit(tmp_path: Path):
    shim = tmp_path / "fake-openclaw"
    shim.write_text("#!/bin/sh\nexit 9\n")
    shim.chmod(0o755)

    exec_ = OpenclawExecutor(bin_path=str(shim))
    events = [e async for e in exec_.run(task_id="t-3", prompt="x")]
    finish = [e for e in events if e[0] == "finish"]
    assert finish == [("finish", "9")]


def test_openclaw_rejects_empty_bin():
    with pytest.raises(ValueError):
        OpenclawExecutor(bin_path="")
