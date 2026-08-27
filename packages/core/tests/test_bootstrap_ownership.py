"""Tests for P3.1.4 — bootstrap-ownership one-shot endpoint."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import User, Workspace, WorkspaceMember
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings_github() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev",
        TD_AUTH_MODE="cognito",
    )


def _settings_disabled() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev",
        TD_AUTH_MODE="disabled",
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


def _make_app(settings, sm):
    app = create_app(settings)
    app.state.settings = settings
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return app


@pytest.mark.asyncio
async def test_bootstrap_ownership_claims_all_workspaces():
    """First authenticated user claims all existing workspaces as owner."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    user = await _make_user(sm)
    ws1 = await _make_workspace(sm)
    ws2 = await _make_workspace(sm)

    # Clean up any pre-existing workspace_members rows to ensure bootstrap
    # precondition (count == 0) holds regardless of prior test state.
    from sqlalchemy import delete as sa_delete

    async with sm() as sess:
        await sess.execute(sa_delete(WorkspaceMember))
        await sess.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: user
        r = await ac.post("/api/v1/auth/bootstrap-ownership")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claimed"] >= 2  # at least our two workspaces

    # Verify WorkspaceMember rows were inserted
    async with sm() as sess:
        m1 = await sess.get(WorkspaceMember, (ws1.id, user.id))
        m2 = await sess.get(WorkspaceMember, (ws2.id, user.id))
    assert m1 is not None
    assert m1.role == "owner"
    assert m2 is not None
    assert m2.role == "owner"


@pytest.mark.asyncio
async def test_bootstrap_ownership_second_call_returns_400():
    """Bootstrap returns 400 if workspace_members rows already exist."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    user = await _make_user(sm)
    await _make_workspace(sm)

    # Clean up pre-existing workspace_members rows.
    from sqlalchemy import delete as sa_delete

    async with sm() as sess:
        await sess.execute(sa_delete(WorkspaceMember))
        await sess.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: user
        # First call — should succeed
        r = await ac.post("/api/v1/auth/bootstrap-ownership")
        assert r.status_code == 200, r.text

        # Second call — already bootstrapped
        r = await ac.post("/api/v1/auth/bootstrap-ownership")
        assert r.status_code == 400
        assert "already completed" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_bootstrap_ownership_disabled_mode_returns_400():
    """Bootstrap returns 400 when auth_mode=disabled."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_disabled()
    app = _make_app(settings, sm)

    from taskdeck_core.auth.middleware import ServicePrincipal
    sp = ServicePrincipal(kind="legacy_single_user")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: sp
        r = await ac.post("/api/v1/auth/bootstrap-ownership")
    assert r.status_code == 400
    assert "auth disabled" in r.json()["detail"].lower()
