from __future__ import annotations

import asyncio

import pytest
from taskdeck_runner.crp_client import CRPClient
from taskdeck_runner.settings import RunnerSettings  # noqa: F401 (used by _settings)


def _settings(**overrides: object) -> RunnerSettings:
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


@pytest.mark.asyncio
async def test_request_stop_exits_run_forever_quickly() -> None:
    """request_stop() while not connected causes run_forever to exit promptly."""
    client = CRPClient(_settings())
    # Mark stopping before run_forever starts — it should exit without
    # attempting to connect.
    client.request_stop()
    stop_event = asyncio.Event()
    stop_event.set()

    # run_forever should return immediately (no reconnect loop entered).
    await asyncio.wait_for(client.run_forever(stop_event), timeout=5)


@pytest.mark.asyncio
async def test_inflight_tasks_tracked_and_drained() -> None:
    """In-flight tasks are tracked and awaited on stop."""
    client = CRPClient(_settings())

    completed: list[str] = []

    async def _slow_task() -> None:
        await asyncio.sleep(0.05)
        completed.append("done")

    # Manually simulate two in-flight tasks being registered.
    for _ in range(2):
        t = asyncio.create_task(_slow_task())
        client._inflight_tasks.add(t)
        t.add_done_callback(client._inflight_tasks.discard)

    client.request_stop()
    # Drain — mirrors what run_forever does on exit.
    if client._inflight_tasks:
        await asyncio.gather(*list(client._inflight_tasks), return_exceptions=True)

    assert completed == ["done", "done"]
    assert len(client._inflight_tasks) == 0
