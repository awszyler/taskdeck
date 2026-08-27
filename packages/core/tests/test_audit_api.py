"""Tests for GET /api/v1/audit."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.audit.sink import AuditEventSink
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import User, Workspace, WorkspaceMember
from taskdeck_core.main import create_app


async def _make_user(sm) -> User:
    async with sm() as sess:
        u = User(
            workspace_id=None,
            email=f"{uuid4().hex[:8]}@test.com",
            name="Audit User",
            role="member",
            cognito_sub=uuid4().hex,
            login=uuid4().hex[:8],
        )
        sess.add(u)
        await sess.commit()
        await sess.refresh(u)
        return u


async def _make_workspace_with_member(sm, user: User) -> Workspace:
    async with sm() as sess:
        ws = Workspace(slug=f"aud-api-{uuid4().hex[:8]}", name="audit-api-test")
        sess.add(ws)
        await sess.flush()
        sess.add(WorkspaceMember(
            workspace_id=ws.id,
            user_id=user.id,
            role="owner",
            created_at=datetime.now(UTC),
        ))
        await sess.commit()
        await sess.refresh(ws)
        return ws


async def _seed_audit_events(sm, ws_id, user_id) -> None:
    sink = AuditEventSink(sessionmaker=sm)
    await sink.handle({
        "type": "audit.event",
        "kind": "workspace.create",
        "user_id": str(user_id),
        "workspace_id": str(ws_id),
        "target_type": "workspace",
        "target_id": str(ws_id),
        "meta": {"slug": "test"},
    })
    await sink.handle({
        "type": "audit.event",
        "kind": "invite.issue",
        "user_id": str(user_id),
        "workspace_id": str(ws_id),
        "target_type": "invite",
        "meta": {"code_prefix": "abcd"},
    })


def _make_app(sm):
    app = create_app()
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return app


@pytest.mark.asyncio
async def test_audit_list_returns_rows():
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, user)
    await _seed_audit_events(sm, ws.id, user.id)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/audit?workspace_id={ws.id}")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) >= 2
    kinds = {i["kind"] for i in items}
    assert "workspace.create" in kinds
    assert "invite.issue" in kinds


@pytest.mark.asyncio
async def test_audit_kind_filter():
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, user)
    await _seed_audit_events(sm, ws.id, user.id)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/audit?workspace_id={ws.id}&kind=invite.issue")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert all(i["kind"] == "invite.issue" for i in items)
    assert len(items) >= 1


@pytest.mark.asyncio
async def test_audit_scoped_to_workspace():
    """Non-member gets 403."""
    sm = await get_sessionmaker_for_tests()
    owner = await _make_user(sm)
    outsider = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, owner)
    await _seed_audit_events(sm, ws.id, owner.id)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: outsider

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/audit?workspace_id={ws.id}")

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_audit_limit_respected():
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, user)
    await _seed_audit_events(sm, ws.id, user.id)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/audit?workspace_id={ws.id}&limit=1")

    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1
