from __future__ import annotations

import asyncio

import pytest
from taskdeck_runner.agents.shell import ShellExecutor


@pytest.mark.asyncio
async def test_run_streams_stdout_and_exits_zero():
    exec_ = ShellExecutor()
    events: list[tuple[str, str]] = []
    async for kind, data in exec_.run(task_id="t-1", prompt="echo hello"):
        events.append((kind, data))

    streams = [e for e in events if e[0] in {"stdout", "stderr"}]
    assert any("hello" in data for _, data in streams)
    finish = [e for e in events if e[0] == "finish"]
    assert len(finish) == 1
    assert finish[0][1] == "0"


@pytest.mark.asyncio
async def test_run_nonzero_exit_reported():
    exec_ = ShellExecutor()
    events = [e async for e in exec_.run(task_id="t-2", prompt="exit 3")]
    finish = [e for e in events if e[0] == "finish"]
    assert finish[0][1] == "3"


@pytest.mark.asyncio
async def test_run_survives_oversized_single_line():
    """Regression: a stdout line larger than asyncio's default 64 KB
    StreamReader limit must NOT deadlock the executor.

    Before the fix (commit after 383578d), task 90207737 wedged a
    runner permanently because claude-code emitted a >64 KB stream-json
    line containing a base64-encoded image; the readline raised
    LimitOverrunError inside the pump, the sentinel was never put on
    the queue, and the consumer hung on q.get() forever.
    """
    async def collect():
        return [
            e async for e in ShellExecutor().run(
                task_id="t-big",
                prompt="python3 -c 'print(\"x\" * 200_000)'",
            )
        ]
    # If the pump deadlocks, asyncio.wait_for fails the test loudly
    # instead of letting pytest hang for its global timeout.
    events = await asyncio.wait_for(collect(), timeout=15)
    finish = [e for e in events if e[0] == "finish"]
    assert len(finish) == 1
    assert finish[0][1] == "0"
