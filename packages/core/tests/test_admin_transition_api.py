"""Tests for M6.1 — POST /api/v1/tasks/{id}/transition access matrix."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, User, Workspace, WorkspaceMember
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_disabled() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev",
    )


def _settings_github() -> Settings:
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


async def _make_user(sm, *, role: str = "member") -> User:
    async with sm() as sess:
        u = User(
            workspace_id=None,
            email=f"{uuid4().hex[:8]}@test.com",
            name="Test User",
            role=role,
            cognito_sub=uuid4().hex,
            login=uuid4().hex[:8],
        )
        sess.add(u)
        await sess.commit()
        await sess.refresh(u)
        return u


async def _make_workspace_with_members(sm, owner: User, members: list[User] | None = None) -> Workspace:
    async with sm() as sess:
        ws = Workspace(slug=f"ws-{uuid4().hex[:8]}", name="test")
        sess.add(ws)
        await sess.flush()
        sess.add(WorkspaceMember(
            workspace_id=ws.id,
            user_id=owner.id,
            role="owner",
            created_at=datetime.now(UTC),
        ))
        for m in (members or []):
            sess.add(WorkspaceMember(
                workspace_id=ws.id,
                user_id=m.id,
                role="member",
                created_at=datetime.now(UTC),
            ))
        await sess.commit()
        await sess.refresh(ws)
        return ws


async def _make_draft_task(sm, workspace_id) -> Task:
    async with sm() as sess:
        task = Task(
            workspace_id=workspace_id,
            title="test task",
            prompt="echo hi",
            origin="web",
            agent="shell",
            status="pending",
        )
        sess.add(task)
        await sess.commit()
        await sess.refresh(task)
        return task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_mode_transition_draft_to_done():
    """auth_mode=disabled: any transition (even illegal draft→done) succeeds."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_disabled()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    ws = await _make_workspace_with_members(sm, owner)
    task = await _make_draft_task(sm, ws.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.post(f"/api/v1/tasks/{task.id}/transition", json={"to": "done"})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"


@pytest.mark.asyncio
async def test_disabled_mode_invalid_target_value_422():
    """auth_mode=disabled: invalid 'to' value rejected by Pydantic regex → 422."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_disabled()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    ws = await _make_workspace_with_members(sm, owner)
    task = await _make_draft_task(sm, ws.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.post(f"/api/v1/tasks/{task.id}/transition", json={"to": "flying"})

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_disabled_mode_missing_task_404():
    """auth_mode=disabled: unknown task_id → 404."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_disabled()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.post(f"/api/v1/tasks/{uuid4()}/transition", json={"to": "done"})

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_github_mode_service_principal_bearer_403():
    """github mode + ServicePrincipal (runner bearer) → 403."""
    from taskdeck_core.auth.middleware import ServicePrincipal

    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    ws = await _make_workspace_with_members(sm, owner)
    task = await _make_draft_task(sm, ws.id)

    runner_principal = ServicePrincipal(kind="service_token")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: runner_principal
        r = await ac.post(f"/api/v1/tasks/{task.id}/transition", json={"to": "done"})

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_github_mode_non_owner_illegal_transition_409():
    """github mode + non-owner member + illegal target (draft→done) → 409."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_members(sm, owner, members=[member])
    task = await _make_draft_task(sm, ws.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/transition", json={"to": "done"})

    assert r.status_code == 409


@pytest.mark.asyncio
async def test_github_mode_non_owner_legal_transition_200():
    """github mode + non-owner member + legal target (draft→cancelled) → 200."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    member = await _make_user(sm)
    ws = await _make_workspace_with_members(sm, owner, members=[member])
    task = await _make_draft_task(sm, ws.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post(f"/api/v1/tasks/{task.id}/transition", json={"to": "cancelled"})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_github_mode_owner_illegal_transition_200():
    """github mode + owner + illegal target (done→running) → 200 (admin override)."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    ws = await _make_workspace_with_members(sm, owner)
    # Create task in 'done' state directly.
    async with sm() as sess:
        task = Task(
            workspace_id=ws.id,
            title="done task",
            prompt="echo hi",
            origin="web",
            agent="shell",
            status="done",
        )
        sess.add(task)
        await sess.commit()
        await sess.refresh(task)
        task_id = task.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.post(f"/api/v1/tasks/{task_id}/transition", json={"to": "running"})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"


@pytest.mark.asyncio
async def test_task_in_other_workspace_returns_404():
    """Task belonging to another workspace is hidden (404) regardless of auth mode."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    other_owner = await _make_user(sm)
    # owner has access to ws1 (their own), task lives in ws2 (other_owner's workspace).
    await _make_workspace_with_members(sm, owner)
    ws2 = await _make_workspace_with_members(sm, other_owner)
    task = await _make_draft_task(sm, ws2.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.post(f"/api/v1/tasks/{task.id}/transition", json={"to": "cancelled"})

    assert r.status_code == 404
