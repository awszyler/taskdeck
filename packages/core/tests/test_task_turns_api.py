"""Tests for GET /api/v1/tasks/{task_id}/turns."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, TaskTurn, User, Workspace, WorkspaceMember
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


async def _make_task_with_turns(sm, workspace_id: UUID, *, turns: list[tuple[str, str]]) -> Task:
    async with sm() as sess:
        task = Task(
            workspace_id=workspace_id,
            title="t", prompt="p", origin="web", agent="claude-code",
            status="awaiting_input",
        )
        sess.add(task)
        await sess.flush()
        for i, (role, content) in enumerate(turns):
            sess.add(TaskTurn(
                task_id=task.id, seq=i, role=role, content=content,
                created_at=datetime.now(UTC),
            ))
        await sess.commit()
        await sess.refresh(task)
        return task


@pytest.mark.asyncio
async def test_list_turns_empty_for_new_task():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task_with_turns(sm, ws.id, turns=[])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/tasks/{task.id}/turns")

    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}


@pytest.mark.asyncio
async def test_list_turns_returns_chronological():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task_with_turns(
        sm, ws.id,
        turns=[("agent", "may I read?"), ("user", "yes"), ("agent", "what file?")],
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.get(f"/api/v1/tasks/{task.id}/turns")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 3
    assert [t["seq"] for t in items] == [0, 1, 2]
    assert [t["role"] for t in items] == ["agent", "user", "agent"]
    assert items[0]["content"] == "may I read?"


@pytest.mark.asyncio
async def test_list_turns_foreign_workspace_returns_404():
    """Anti-enumeration matches GET /tasks/{id}."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    owner = await _make_user(sm)
    foreign_ws = await _make_workspace_with_member(sm, owner)
    task = await _make_task_with_turns(sm, foreign_ws.id, turns=[("agent", "x")])

    intruder = await _make_user(sm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: intruder
        r = await ac.get(f"/api/v1/tasks/{task.id}/turns")

    assert r.status_code == 404, r.text
