from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import TaskLog, Workspace
from taskdeck_core.main import create_app


@pytest.mark.asyncio
async def test_submit_task_runs_on_runner_and_reaches_done(monkeypatch):
    # Use the dev runner token so the WS handshake succeeds.
    monkeypatch.setenv("TD_RUNNER_BEARER_TOKEN", "e2e-token")
    monkeypatch.setenv("TD_CORE_WS_URL", "ws://127.0.0.1:18080/api/v1/crp/connect")
    monkeypatch.setenv("TD_RUNNER_TOKEN", "e2e-token")
    monkeypatch.setenv("TD_RUNNER_NAME", "e2e-runner")
    monkeypatch.setenv("TD_MAX_PARALLEL", "1")

    # Start core via uvicorn in-process.
    import uvicorn

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=18080, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # Wait for server up
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)

    # Bootstrap workspace using a fresh sessionmaker (lifespan-scoped one is on the app).
    # Use a unique slug to avoid conflicts when the suite runs multiple times.
    import uuid as _uuid
    e2e_slug = f"e2e-{_uuid.uuid4().hex[:8]}"
    sm = await get_sessionmaker_for_tests()
    async with sm() as sess:
        ws = Workspace(slug=e2e_slug, name="e2e")
        sess.add(ws)
        await sess.commit()
        ws_id = str(ws.id)

    # Start runner
    from taskdeck_runner.crp_client import CRPClient
    from taskdeck_runner.settings import RunnerSettings

    runner_settings = RunnerSettings()  # type: ignore[call-arg]
    runner = CRPClient(runner_settings)
    runner_task = asyncio.create_task(runner.run_forever())

    # Create + submit task
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.post(
            "/api/v1/tasks",
            json={
                "workspace_id": ws_id,
                "title": "echo",
                "prompt": "echo e2e-marker",
                "origin": "web",
                "agent": "shell",
            },
        )
        tid = r.json()["id"]
        await ac.post(f"/api/v1/tasks/{tid}/submit")

        # Poll until done
        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            g = await ac.get(f"/api/v1/tasks/{tid}")
            status = g.json()["status"]
            if status in {"done", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.2)

    # Assert log captured — use same sessionmaker for post-test assertions
    async with sm() as sess:
        logs = (
            await sess.scalars(select(TaskLog).order_by(TaskLog.seq))
        ).all()
        assert any("e2e-marker" in log_.data for log_ in logs)

    assert status == "done"

    runner_task.cancel()
    server.should_exit = True
    await asyncio.gather(server_task, runner_task, return_exceptions=True)
