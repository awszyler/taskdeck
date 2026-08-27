"""Test KiroCliExecutor: ANSI stripping + subprocess invocation shape."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskdeck_runner.agents.kiro_cli import KiroCliExecutor, _strip_ansi


def test_strip_ansi_removes_color_codes():
    s = "\x1b[32mhello\x1b[0m world\x1b[1;31m!\x1b[0m"
    assert _strip_ansi(s) == "hello world!"


def test_strip_ansi_passthrough_when_no_escapes():
    assert _strip_ansi("plain text") == "plain text"


def test_init_rejects_empty_bin_path():
    with pytest.raises(ValueError, match="non-empty"):
        KiroCliExecutor("")


@pytest.mark.asyncio
async def test_run_invokes_correct_subprocess_args(tmp_path):
    """Verify the subprocess is launched with chat --no-interactive --trust-all-tools <prompt>."""
    fake_proc = MagicMock()
    fake_proc.stdout = MagicMock()
    fake_proc.stderr = MagicMock()

    # Each readline returns a single line then EOF.
    stdout_lines = [b"\x1b[32mline1\x1b[0m\n", b""]
    stderr_lines = [b""]
    fake_proc.stdout.readline = AsyncMock(side_effect=stdout_lines)
    fake_proc.stderr.readline = AsyncMock(side_effect=stderr_lines)
    fake_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)) as spawn:
        ex = KiroCliExecutor("/fake/kiro-cli")
        outputs = []
        async for item in ex.run(task_id="t-1", prompt="hello world", cwd=tmp_path):
            outputs.append(item)

        # Subprocess args are correct.
        spawn.assert_called_once()
        args = spawn.call_args.args
        assert args == (
            "/fake/kiro-cli", "chat", "--no-interactive", "--trust-all-tools", "hello world",
        )

    # ANSI stripped on stdout; finish event present.
    cleaned_stdout = [s for label, s in outputs if label == "stdout"]
    assert "line1" in cleaned_stdout
    assert ("finish", "0") in outputs
