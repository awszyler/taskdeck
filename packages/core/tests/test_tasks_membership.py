"""Regression: POST /api/v1/tasks must check workspace membership.

Without this check a stale activeWorkspaceId in the client would let
create_task return 201 for a workspace whose tasks the same user can't
read — silent-success on the UI, no card on the kanban.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import User, Workspace, WorkspaceMember
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


async def _make_workspace(sm, owner: User | None = None) -> Workspace:
    async with sm() as sess:
        ws = Workspace(slug=f"ws-{uuid4().hex[:8]}", name="test")
        sess.add(ws)
        await sess.flush()
        if owner is not None:
            sess.add(WorkspaceMember(
                workspace_id=ws.id,
                user_id=owner.id,
                role="owner",
                created_at=datetime.now(UTC),
            ))
        await sess.commit()
        await sess.refresh(ws)
        return ws


@pytest.mark.asyncio
async def test_create_task_in_foreign_workspace_returns_403():
    """Authenticated user posting into a workspace they don't belong to → 403."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    other_user = await _make_user(sm)
    foreign_ws = await _make_workspace(sm, owner=other_user)
    intruder = await _make_user(sm)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: intruder
        r = await ac.post("/api/v1/tasks", json={
            "workspace_id": str(foreign_ws.id),
            "title": "should be rejected",
            "prompt": "echo hi",
            "origin": "web",
            "agent": "shell",
        })

    assert r.status_code == 403, r.text
    assert "member" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_task_in_member_workspace_succeeds():
    """Member of the workspace gets 201 — the membership check doesn't break the happy path."""
    sm = await get_sessionmaker_for_tests()
    app = _make_app(_settings_cognito(), sm)

    member = await _make_user(sm)
    ws = await _make_workspace(sm, owner=member)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: member
        r = await ac.post("/api/v1/tasks", json={
            "workspace_id": str(ws.id),
            "title": "ok",
            "prompt": "echo hi",
            "origin": "web",
            "agent": "shell",
        })

    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
