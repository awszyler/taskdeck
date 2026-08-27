"""Tests for P3.1.4 — workspace members + invites API."""

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


async def _make_workspace_with_owner(sm, owner: User) -> Workspace:
    """Create a workspace via API so auto-membership fires."""
    # We do it directly in DB to avoid needing a full HTTP round-trip for setup.
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
async def test_create_workspace_auto_adds_owner_member():
    """POST /workspaces with github auth auto-inserts an owner WorkspaceMember row."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    slug = f"auto-{uuid4().hex[:8]}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.post("/api/v1/workspaces", json={"slug": slug, "name": "Auto"})

    assert r.status_code == 201, r.text
    ws_id = r.json()["id"]

    from uuid import UUID

    from taskdeck_core.db.models import WorkspaceMember as WM

    async with sm() as sess:
        member = await sess.get(WM, (UUID(ws_id), owner.id))
    assert member is not None
    assert member.role == "owner"


@pytest.mark.asyncio
async def test_owner_creates_invite_member_joins():
    """Owner issues invite, other user consumes it, becomes member."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    member_user = await _make_user(sm)
    ws = await _make_workspace_with_owner(sm, owner)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        # Owner creates invite
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.post(f"/api/v1/workspaces/{ws.id}/invites")
        assert r.status_code == 200, r.text
        code = r.json()["code"]
        assert len(code) > 0

        # Member user joins
        app.dependency_overrides[current_principal] = lambda: member_user
        r = await ac.post("/api/v1/workspaces/join", json={"code": code})
        assert r.status_code == 204, r.text

    from taskdeck_core.db.models import WorkspaceMember as WM

    async with sm() as sess:
        m = await sess.get(WM, (ws.id, member_user.id))
    assert m is not None
    assert m.role == "member"


@pytest.mark.asyncio
async def test_member_cannot_remove_owner():
    """Non-owner member cannot remove the workspace owner."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    regular = await _make_user(sm)
    ws = await _make_workspace_with_owner(sm, owner)

    # Add regular as member
    async with sm() as sess:
        sess.add(WorkspaceMember(
            workspace_id=ws.id,
            user_id=regular.id,
            role="member",
            created_at=datetime.now(UTC),
        ))
        await sess.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: regular
        r = await ac.delete(f"/api/v1/workspaces/{ws.id}/members/{owner.id}")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_owner_can_remove_regular_member():
    """Owner can remove a non-owner member."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    regular = await _make_user(sm)
    ws = await _make_workspace_with_owner(sm, owner)

    async with sm() as sess:
        sess.add(WorkspaceMember(
            workspace_id=ws.id,
            user_id=regular.id,
            role="member",
            created_at=datetime.now(UTC),
        ))
        await sess.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.delete(f"/api/v1/workspaces/{ws.id}/members/{regular.id}")
    assert r.status_code == 204

    from taskdeck_core.db.models import WorkspaceMember as WM

    async with sm() as sess:
        m = await sess.get(WM, (ws.id, regular.id))
    assert m is None


@pytest.mark.asyncio
async def test_owner_cannot_remove_themselves_if_sole_owner():
    """Sole owner cannot remove themselves — would leave workspace ownerless."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    ws = await _make_workspace_with_owner(sm, owner)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.delete(f"/api/v1/workspaces/{ws.id}/members/{owner.id}")
    assert r.status_code == 400
    assert "last owner" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_list_members():
    """GET /workspaces/{id}/members returns members of the workspace."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    owner = await _make_user(sm)
    ws = await _make_workspace_with_owner(sm, owner)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: owner
        r = await ac.get(f"/api/v1/workspaces/{ws.id}/members")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["user_id"] == str(owner.id)
    assert items[0]["role"] == "owner"


@pytest.mark.asyncio
async def test_invalid_invite_code_returns_400():
    """Joining with a nonexistent code returns 400."""
    sm = await get_sessionmaker_for_tests()
    settings = _settings_github()
    app = _make_app(settings, sm)

    user = await _make_user(sm)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        app.dependency_overrides[current_principal] = lambda: user
        r = await ac.post("/api/v1/workspaces/join", json={"code": "doesnotexist"})
    assert r.status_code == 400
