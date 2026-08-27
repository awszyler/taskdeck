"""Tests for core's /api/v1/sandbox/* endpoints (P6.3.2).

Mocks sandbox-host with respx so tests don't require docker.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import (
    Sandbox,
    Task,
    User,
    Workspace,
    WorkspaceMember,
)
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings_cognito() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev",
        TD_AUTH_MODE="cognito",
        TD_SANDBOX_HOST_URL="http://test-sandbox-host:9101",
    )


def _make_app(settings, sm):
    app = create_app(settings)
    app.state.settings = settings
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return app


async def _make_user(sm) -> User:
    async with sm() as sess:
        u = User(
            workspace_id=None,
            email=f"{uuid4().hex[:8]}@test.com",
            name="Test User",
            role="member",
            cognito_sub=uuid4().hex,
            login=uuid4().hex[:8],
        )
        sess.add(u)
        await sess.commit()
        await sess.refresh(u)
        return u


async def _make_workspace_with_member(sm, member: User) -> Workspace:
    async with sm() as sess:
        ws = Workspace(slug=f"ws-{uuid4().hex[:8]}", name="test")
        sess.add(ws)
        await sess.flush()
        sess.add(WorkspaceMember(
            workspace_id=ws.id,
            user_id=member.id,
            role="owner",
            created_at=datetime.now(UTC),
        ))
        await sess.commit()
        await sess.refresh(ws)
        return ws


async def _make_task(sm, workspace_id: UUID, *, status: str) -> Task:
    async with sm() as sess:
        task = Task(
            workspace_id=workspace_id,
            title="t", prompt="p", origin="web", agent="claude-code",
            status=status,
        )
        sess.add(task)
        await sess.commit()
        await sess.refresh(task)
        return task


# ------- /start -------------------------------------------------------


@pytest.mark.asyncio
async def test_start_happy_path():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    with respx.mock(base_url=settings.sandbox_host_url) as router:
        router.post("/provision").mock(return_value=Response(
            200,
            json={
                "task_id": str(task.id),
                "host_port": 32811,
                "runtime": "static",
                "image": "td-sandbox-static:latest",
                "base_path": f"/sandbox/{task.id}/",
            },
        ))

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
        ) as ac:
            app.dependency_overrides[current_principal] = lambda: member
            r = await ac.post(f"/api/v1/sandbox/{task.id}/start")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base_path"] == f"/sandbox/{task.id}/"
    assert body["runtime"] == "static"

    # Sandbox row should be running.
    async with sm() as sess:
        sb = await sess.get(Sandbox, task.id)
    assert sb is not None
    assert sb.status == "running"
    assert sb.host_port == 32811


@pytest.mark.asyncio
async def test_start_rejects_pending_task():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="pending")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/sandbox/{task.id}/start")

    assert r.status_code == 409, r.text
    assert "cannot start sandbox" in r.json()["detail"]


@pytest.mark.asyncio
async def test_start_propagates_429_from_sandbox_host():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    with respx.mock(base_url=settings.sandbox_host_url) as router:
        router.post("/provision").mock(return_value=Response(429, text="full"))

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
        ) as ac:
            app.dependency_overrides[current_principal] = lambda: member
            r = await ac.post(f"/api/v1/sandbox/{task.id}/start")

    assert r.status_code == 429, r.text
    async with sm() as sess:
        sb = await sess.get(Sandbox, task.id)
    assert sb.status == "error"


@pytest.mark.asyncio
async def test_start_502_when_sandbox_host_unreachable():
    sm = await get_sessionmaker_for_tests()
    # Use a URL that respx is mocking — without a registered route,
    # the call raises a ConnectError that we'll convert to 502.
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    with respx.mock(base_url=settings.sandbox_host_url) as router:
        router.post("/provision").side_effect = (
            __import__("httpx").ConnectError("nope")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
        ) as ac:
            app.dependency_overrides[current_principal] = lambda: member
            r = await ac.post(f"/api/v1/sandbox/{task.id}/start")

    assert r.status_code == 502, r.text


@pytest.mark.asyncio
async def test_start_404_for_foreign_workspace():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    _own = await _make_workspace_with_member(sm, member)
    other = await _make_user(sm)
    foreign_ws = await _make_workspace_with_member(sm, other)
    task = await _make_task(sm, foreign_ws.id, status="done")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/sandbox/{task.id}/start")

    assert r.status_code == 404


# ------- /stop -------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_marks_db_row_stopped():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    # Seed a running sandbox row.
    now = datetime.now(UTC)
    async with sm() as sess:
        sess.add(Sandbox(
            task_id=task.id, status="running", host_port=12345,
            runtime="static", base_path=f"/sandbox/{task.id}/",
            started_at=now, created_at=now, updated_at=now,
        ))
        await sess.commit()

    with respx.mock(base_url=settings.sandbox_host_url) as router:
        router.post("/stop").mock(return_value=Response(
            200, json={"found": True},
        ))

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
        ) as ac:
            app.dependency_overrides[current_principal] = lambda: member
            r = await ac.post(f"/api/v1/sandbox/{task.id}/stop")

    assert r.status_code == 200, r.text
    assert r.json()["found"] is True
    async with sm() as sess:
        sb = await sess.get(Sandbox, task.id)
    assert sb.status == "stopped"
    assert sb.stopped_at is not None


# ------- /status ------------------------------------------------------


@pytest.mark.asyncio
async def test_status_synthesizes_not_provisioned():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/sandbox/{task.id}/status")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "not_provisioned"
    assert body["host_port"] is None


@pytest.mark.asyncio
async def test_status_returns_running_row():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    now = datetime.now(UTC)
    async with sm() as sess:
        sess.add(Sandbox(
            task_id=task.id, status="running", host_port=12345,
            runtime="node", base_path=f"/sandbox/{task.id}/",
            started_at=now, created_at=now, updated_at=now,
        ))
        await sess.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/sandbox/{task.id}/status")

    body = r.json()
    assert body["status"] == "running"
    assert body["host_port"] == 12345
    assert body["runtime"] == "node"


# ------- /auth (Caddy forward_auth) ----------------------------------


@pytest.mark.asyncio
async def test_auth_check_passes_for_workspace_member():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/sandbox/auth/{task.id}")

    assert r.status_code == 200


@pytest.mark.asyncio
async def test_auth_check_404_for_non_member():
    sm = await get_sessionmaker_for_tests()
    settings = _settings_cognito()
    app = _make_app(settings, sm)
    member = await _make_user(sm)
    _own = await _make_workspace_with_member(sm, member)
    other = await _make_user(sm)
    foreign_ws = await _make_workspace_with_member(sm, other)
    task = await _make_task(sm, foreign_ws.id, status="done")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/sandbox/auth/{task.id}")

    assert r.status_code == 404
