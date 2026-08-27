"""Tests for GET /api/v1/tasks/{task_id}/logs.

Returns the most recent log lines for a task. Used by the kanban
detail drawer.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, TaskLog, User, Workspace, WorkspaceMember
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings_cognito() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev",
        TD_AUTH_MODE="cognito",
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


async def _make_task_with_logs(
    sm,
    workspace_id: UUID,
    *,
    stdout_count: int = 0,
    stderr_count: int = 0,
) -> Task:
    """Insert a task plus N stdout lines + M stderr lines, interleaved by seq."""
    async with sm() as sess:
        task = Task(
            workspace_id=workspace_id,
            title="t", prompt="echo hi", origin="web", agent="shell",
            status="done", exit_code=0,
        )
        sess.add(task)
        await sess.flush()
        seq = 0
        for i in range(stdout_count):
            seq += 1
            sess.add(TaskLog(
                task_id=task.id, seq=seq, stream="stdout",
                data=f"out-{i}", created_at=datetime.now(UTC),
            ))
        for i in range(stderr_count):
            seq += 1
            sess.add(TaskLog(
                task_id=task.id, seq=seq, stream="stderr",
                data=f"err-{i}", created_at=datetime.now(UTC),
            ))
        await sess.commit()
        await sess.refresh(task)
        return task


@pytest.mark.asyncio
async def test_list_logs_returns_chronological():
    """Five lines inserted in order should come back in seq-ascending order."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task_with_logs(sm, ws.id, stdout_count=5)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/tasks/{task.id}/logs")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 5
    assert body["returned"] == 5
    assert body["truncated"] is False
    assert [item["seq"] for item in body["items"]] == [1, 2, 3, 4, 5]
    assert body["items"][0]["data"] == "out-0"


@pytest.mark.asyncio
async def test_list_logs_truncates_to_limit():
    """When total > limit, returns the most recent N lines (chronological) and sets truncated=True."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task_with_logs(sm, ws.id, stdout_count=10)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/tasks/{task.id}/logs?limit=3")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 10
    assert body["returned"] == 3
    assert body["truncated"] is True
    assert [item["seq"] for item in body["items"]] == [8, 9, 10]


@pytest.mark.asyncio
async def test_list_logs_filters_by_stream():
    """?stream=stderr returns only stderr rows."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task_with_logs(sm, ws.id, stdout_count=3, stderr_count=2)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/tasks/{task.id}/logs?stream=stderr")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert all(item["stream"] == "stderr" for item in body["items"])
    assert [item["data"] for item in body["items"]] == ["err-0", "err-1"]


@pytest.mark.asyncio
async def test_list_logs_foreign_workspace_returns_404():
    """Non-member sees 404, not 403 — anti-enumeration matches GET /tasks/{id}."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    owner = await _make_user(sm)
    foreign_ws = await _make_workspace_with_member(sm, owner)
    task = await _make_task_with_logs(sm, foreign_ws.id, stdout_count=1)

    intruder = await _make_user(sm)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: intruder
        r = await ac.get(f"/api/v1/tasks/{task.id}/logs")

    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_list_logs_task_not_found_returns_404():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    await _make_workspace_with_member(sm, member)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/tasks/{uuid4()}/logs")

    assert r.status_code == 404, r.text
