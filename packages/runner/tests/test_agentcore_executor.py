from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from taskdeck_runner.agents.agentcore import AgentCoreExecutor

if TYPE_CHECKING:
    from pathlib import Path


class _FakeClient:
    """Mock boto3 client that returns a pre-defined completion stream."""

    def __init__(self, *, chunks: list[bytes], exc: Exception | None = None):
        self._chunks = chunks
        self._exc = exc
        self.invoked_with: dict | None = None

    def invoke_agent(self, **kwargs: object) -> dict:
        self.invoked_with = kwargs
        if self._exc:
            raise self._exc
        return {
            "completion": [{"chunk": {"bytes": c}} for c in self._chunks],
        }


@pytest.mark.asyncio
async def test_forwards_chunks_and_reports_success(tmp_path: Path) -> None:
    fake = _FakeClient(chunks=[b"hello ", b"world"])
    ex = AgentCoreExecutor(agent_id="FOO", region="ap-northeast-1", client=fake)
    events = [e async for e in ex.run(task_id="t-1", prompt="hi")]

    streams = [e for e in events if e[0] in {"stdout", "stderr"}]
    finish = [e for e in events if e[0] == "finish"]

    assert streams == [("stdout", "hello "), ("stdout", "world")]
    assert finish == [("finish", "0")]
    assert ex.last_full_output == "hello world"
    assert fake.invoked_with is not None
    assert fake.invoked_with["agentId"] == "FOO"
    assert fake.invoked_with["sessionId"] == "t-1"


@pytest.mark.asyncio
async def test_slices_large_chunk(tmp_path: Path) -> None:
    big = "x" * 5000
    fake = _FakeClient(chunks=[big.encode()])
    ex = AgentCoreExecutor(agent_id="FOO", region="ap-northeast-1", client=fake)
    events = [e async for e in ex.run(task_id="t-2", prompt="hi")]
    stdout = [d for k, d in events if k == "stdout"]
    assert len(stdout) == 3  # 2048 + 2048 + 904
    assert "".join(stdout) == big


@pytest.mark.asyncio
async def test_reports_failure_on_client_exception(tmp_path: Path) -> None:
    fake = _FakeClient(chunks=[], exc=RuntimeError("AccessDenied"))
    ex = AgentCoreExecutor(agent_id="FOO", region="ap-northeast-1", client=fake)
    events = [e async for e in ex.run(task_id="t-3", prompt="hi")]
    finish = [e for e in events if e[0] == "finish"]
    assert finish == [("finish", "1")]
    assert any("AccessDenied" in d for k, d in events if k == "stderr")
    assert ex.last_full_output == ""
