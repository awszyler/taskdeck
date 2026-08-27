"""Test the reject_offline_agents safety net on POST /api/v1/tasks."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.crp.hub import RunnerConnection, RunnerHub
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import Workspace
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


class _FakeSocket:
    async def send_json(self, _: dict) -> None: ...


async def _setup_app_with_workspace():
    slug = f"test-{uuid.uuid4().hex[:8]}"
    sm = await get_sessionmaker_for_tests()
    async with sm() as sess:
        ws = Workspace(slug=slug, name=slug)
        sess.add(ws)
        await sess.commit()
        ws_id = str(ws.id)

    app = create_app()
    app.state.db_sessionmaker = sm
    # ASGITransport doesn't run lifespan; pre-seed settings.
    app.state.settings = Settings()  # type: ignore[call-arg]

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return app, sm, ws_id


@pytest.mark.asyncio
async def test_offline_agent_rejected_when_setting_enabled():
    app, _sm, ws_id = await _setup_app_with_workspace()

    # Flip the guard ON for this test only.
    app.state.settings = app.state.settings.model_copy(
        update={"reject_offline_agents": True}
    )

    # Provide a hub with NO runners — every agent should be rejected.
    app.state.runner_hub = RunnerHub()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/tasks",
            json={
                "workspace_id": ws_id,
                "title": "echo hi",
                "prompt": "echo hi",
                "origin": "web",
                "agent": "claude-code",
            },
        )
        assert r.status_code == 400
        assert "no connected runner" in r.json()["detail"]


@pytest.mark.asyncio
async def test_known_agent_accepted_when_setting_enabled():
    app, _sm, ws_id = await _setup_app_with_workspace()
    app.state.settings = app.state.settings.model_copy(
        update={"reject_offline_agents": True}
    )

    hub = RunnerHub()
    hub.register(RunnerConnection(
        "r-1", _FakeSocket(), 1, ["shell", "claude-code"],  # type: ignore[arg-type]
        capability_descriptions={"shell": "S", "claude-code": "CC"},
    ))
    app.state.runner_hub = hub

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/tasks",
            json={
                "workspace_id": ws_id,
                "title": "fix bug",
                "prompt": "fix the failing test",
                "origin": "web",
                "agent": "claude-code",
            },
        )
        assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_default_setting_off_allows_offline_agent():
    """Default behavior: queue tasks even before runner is online."""
    app, _sm, ws_id = await _setup_app_with_workspace()
    # No runner_hub set — and reject_offline_agents defaults to False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/tasks",
            json={
                "workspace_id": ws_id,
                "title": "queued",
                "prompt": "echo queued",
                "origin": "web",
                "agent": "kiro-cli",  # nothing connected
            },
        )
        assert r.status_code == 201
