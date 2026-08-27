"""Tests for POST /api/v1/tasks/{task_id}/respond."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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


async def _make_task(sm, workspace_id: UUID, *, status: str, agent_turn: str | None = None) -> Task:
    async with sm() as sess:
        task = Task(
            workspace_id=workspace_id,
            title="t", prompt="p", origin="web", agent="claude-code",
            status=status,
        )
        sess.add(task)
        await sess.flush()
        if agent_turn is not None:
            sess.add(TaskTurn(
                task_id=task.id, seq=0, role="agent", content=agent_turn,
                created_at=datetime.now(UTC),
            ))
        await sess.commit()
        await sess.refresh(task)
        return task


@pytest.mark.asyncio
async def test_respond_writes_user_turn_and_pends_task():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="awaiting_input", agent_turn="may I read?")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/respond", json={"content": "yes please"})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"

    async with sm() as sess:
        rows = (
            await sess.scalars(
                select(TaskTurn).where(TaskTurn.task_id == task.id).order_by(TaskTurn.seq.asc())
            )
        ).all()
        assert len(rows) == 2
        assert rows[1].role == "user"
        assert rows[1].seq == 1
        assert rows[1].content == "yes please"


@pytest.mark.asyncio
async def test_respond_on_running_returns_400():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="running")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/respond", json={"content": "x"})

    assert r.status_code == 400, r.text
    assert "running" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_respond_on_done_returns_400():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/respond", json={"content": "x"})

    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_respond_foreign_workspace_returns_403():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    owner = await _make_user(sm)
    foreign_ws = await _make_workspace_with_member(sm, owner)
    task = await _make_task(sm, foreign_ws.id, status="awaiting_input", agent_turn="x")

    intruder = await _make_user(sm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: intruder
        r = await ac.post(f"/api/v1/tasks/{task.id}/respond", json={"content": "x"})

    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_respond_empty_content_returns_400():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="awaiting_input", agent_turn="x")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        for content in ["", "   ", "\n\t  "]:
            r = await ac.post(f"/api/v1/tasks/{task.id}/respond", json={"content": content})
            assert r.status_code == 400, f"empty {content!r}: {r.text}"


@pytest.mark.asyncio
async def test_respond_too_long_returns_400():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="awaiting_input", agent_turn="x")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/respond", json={"content": "x" * 8193})

    assert r.status_code == 400, r.text
