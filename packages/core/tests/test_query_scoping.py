"""Tests for P3.1.3 — workspace-scoped query filtering."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import ServicePrincipal, current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, User, Workspace, WorkspaceMember
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings_github() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev",
        TD_AUTH_MODE="cognito",
    )


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


async def _make_workspace(sm) -> Workspace:
    async with sm() as sess:
        ws = Workspace(slug=f"ws-{uuid4().hex[:8]}", name="test")
        sess.add(ws)
        await sess.commit()
        await sess.refresh(ws)
        return ws


async def _add_member(sm, workspace_id, user_id, role="member") -> None:
    async with sm() as sess:
        sess.add(WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            created_at=datetime.now(UTC),
        ))
        await sess.commit()


async def _make_task(sm, workspace_id) -> Task:
    async with sm() as sess:
        t = Task(
            workspace_id=workspace_id,
            title=f"task-{uuid4().hex[:6]}",
            prompt="echo hi",
            origin="web",
            agent="shell",
            status="pending",
        )
        sess.add(t)
        await sess.commit()
        await sess.refresh(t)
        return t


@pytest.mark.asyncio
async def test_list_workspaces_scoped_to_member():
    """User A only sees W1; user B only sees W2."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = create_app(settings)
    app.state.settings = settings
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    user_a = await _make_user(sm)
    user_b = await _make_user(sm)
    ws1 = await _make_workspace(sm)
    ws2 = await _make_workspace(sm)

    await _add_member(sm, ws1.id, user_a.id, role="owner")
    await _add_member(sm, ws2.id, user_b.id, role="owner")

    # User A sees only W1
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: user_a
        r = await ac.get("/api/v1/workspaces")

    assert r.status_code == 200
    slugs = {w["slug"] for w in r.json()["items"]}
    assert ws1.slug in slugs
    assert ws2.slug not in slugs


@pytest.mark.asyncio
async def test_list_tasks_scoped_to_member():
    """User A only sees tasks in W1; tasks in W2 are hidden."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = create_app(settings)
    app.state.settings = settings
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    user_a = await _make_user(sm)
    ws1 = await _make_workspace(sm)
    ws2 = await _make_workspace(sm)
    await _add_member(sm, ws1.id, user_a.id, role="owner")

    task_w1 = await _make_task(sm, ws1.id)
    task_w2 = await _make_task(sm, ws2.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: user_a
        r = await ac.get("/api/v1/tasks")

    assert r.status_code == 200
    ids = {t["id"] for t in r.json()["items"]}
    assert str(task_w1.id) in ids
    assert str(task_w2.id) not in ids


@pytest.mark.asyncio
async def test_get_task_in_other_workspace_returns_404():
    """GET /tasks/{id} for a task the user has no access to returns 404."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = create_app(settings)
    app.state.settings = settings
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    user_a = await _make_user(sm)
    ws1 = await _make_workspace(sm)
    ws2 = await _make_workspace(sm)
    await _add_member(sm, ws1.id, user_a.id, role="owner")

    task_in_w2 = await _make_task(sm, ws2.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: user_a
        r = await ac.get(f"/api/v1/tasks/{task_in_w2.id}")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_service_principal_sees_all_workspaces():
    """ServicePrincipal (bearer token) sees all workspaces regardless of memberships."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = create_app(settings)
    app.state.settings = settings
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    ws1 = await _make_workspace(sm)
    ws2 = await _make_workspace(sm)

    sp = ServicePrincipal(kind="service_token")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: sp
        r = await ac.get("/api/v1/workspaces")

    assert r.status_code == 200
    slugs = {w["slug"] for w in r.json()["items"]}
    assert ws1.slug in slugs
    assert ws2.slug in slugs
