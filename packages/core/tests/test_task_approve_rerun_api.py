"""Tests for POST /api/v1/tasks/{task_id}/approve and /rerun.

Approve: in_review → done (user accepts the agent's result).
Rerun:   in_review / done / failed / cancelled → pending. Clean slate —
         deletes prior task_logs and task_turns so the next run's drawer
         shows only fresh output. Resets exit_code, summary, started_at,
         assigned_runner_id. task_events (audit trail) is preserved.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, TaskLog, TaskTurn, User, Workspace, WorkspaceMember
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


@pytest.mark.asyncio
async def test_approve_in_review_transitions_to_done():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="in_review")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/approve")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"


@pytest.mark.asyncio
async def test_approve_running_is_illegal():
    """Approve only legal from in_review."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="running")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/approve")

    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_approve_404_for_foreign_workspace():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    _own_ws = await _make_workspace_with_member(sm, member)
    other = await _make_user(sm)
    foreign_ws = await _make_workspace_with_member(sm, other)
    task = await _make_task(sm, foreign_ws.id, status="in_review")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/approve")

    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_rerun_from_in_review_pends_task():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="in_review")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/rerun")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_rerun_from_done_pends_task():
    """Rerun is also legal from a completed task — supports the 'run again'
    affordance on done cards."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/rerun")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_rerun_from_running_is_illegal():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="running")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/rerun")

    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_rerun_404_for_foreign_workspace():
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    _own_ws = await _make_workspace_with_member(sm, member)
    other = await _make_user(sm)
    foreign_ws = await _make_workspace_with_member(sm, other)
    task = await _make_task(sm, foreign_ws.id, status="in_review")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/rerun")

    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_rerun_clears_prior_logs_and_turns():
    """Rerun is clean-slate: prior task_logs and task_turns are wiped
    so the drawer shows only the new run's output."""
    from sqlalchemy import select
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="done")

    # Seed fake history from the prior run.
    async with sm() as sess:
        sess.add(TaskLog(
            task_id=task.id, seq=0, stream="stdout", data="old output",
            created_at=datetime.now(UTC),
        ))
        sess.add(TaskTurn(
            task_id=task.id, seq=0, role="agent", content="old question",
            created_at=datetime.now(UTC),
        ))
        # Pre-existing task fields that should reset.
        t = await sess.get(Task, task.id)
        assert t is not None
        t.exit_code = 0
        t.summary = "old summary"
        await sess.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/rerun")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"

    # Verify wipe.
    async with sm() as sess:
        log_count = (await sess.scalars(
            select(TaskLog).where(TaskLog.task_id == task.id)
        )).all()
        turn_count = (await sess.scalars(
            select(TaskTurn).where(TaskTurn.task_id == task.id)
        )).all()
        t = await sess.get(Task, task.id)

    assert log_count == [], "task_logs should be wiped"
    assert turn_count == [], "task_turns should be wiped"
    assert t is not None
    assert t.exit_code is None, "exit_code should reset"
    assert t.summary is None, "summary should reset"
    assert t.started_at is None, "started_at should reset"


@pytest.mark.asyncio
async def test_rerun_does_not_wipe_on_illegal_transition():
    """If rerun is rejected (e.g. running task), history must be preserved.
    The validity check runs BEFORE the DELETE statements."""
    from sqlalchemy import select
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, member)
    task = await _make_task(sm, ws.id, status="running")

    async with sm() as sess:
        sess.add(TaskLog(
            task_id=task.id, seq=0, stream="stdout", data="in-flight output",
            created_at=datetime.now(UTC),
        ))
        await sess.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/rerun")

    assert r.status_code == 409, r.text

    # Logs MUST still be there — illegal rerun shouldn't wipe history.
    async with sm() as sess:
        logs = (await sess.scalars(
            select(TaskLog).where(TaskLog.task_id == task.id)
        )).all()
    assert len(logs) == 1
    assert logs[0].data == "in-flight output"
