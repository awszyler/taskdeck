"""Tests for GET /api/v1/costs/summary and GET /api/v1/costs/events."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import CostEvent, User, Workspace, WorkspaceMember
from taskdeck_core.main import create_app


async def _make_user(sm) -> User:
    async with sm() as sess:
        u = User(
            workspace_id=None,
            email=f"{uuid4().hex[:8]}@test.com",
            name="Cost User",
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
        ws = Workspace(slug=f"cst-api-{uuid4().hex[:8]}", name="cost-api-test")
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


async def _seed_cost_events(sm, ws_id, user_id) -> None:
    async with sm() as sess:
        sess.add(CostEvent(
            workspace_id=ws_id,
            user_id=user_id,
            provider="litellm",
            operation="intent_parser",
            model="anthropic/claude-sonnet-4-6",
            tokens_in=1000,
            tokens_out=500,
            cost_usd=Decimal("0.010500"),
            meta={},
            created_at=datetime.now(UTC),
        ))
        sess.add(CostEvent(
            workspace_id=ws_id,
            user_id=user_id,
            provider="litellm",
            operation="stt",
            model="openai/whisper-1",
            cost_usd=Decimal("0.006000"),
            meta={},
            created_at=datetime.now(UTC),
        ))
        await sess.commit()


def _make_app(sm):
    app = create_app()
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return app


@pytest.mark.asyncio
async def test_costs_summary_math():
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, user)
    await _seed_cost_events(sm, ws.id, user.id)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/costs/summary?workspace_id={ws.id}")

    assert r.status_code == 200, r.text
    body = r.json()
    # 0.010500 + 0.006000 = 0.016500
    assert Decimal(body["total_usd"]) == Decimal("0.016500")
    assert "intent_parser" in body["by_operation"]
    assert "stt" in body["by_operation"]
    assert str(user.id) in body["by_user"]
    assert len(body["by_day"]) >= 1


@pytest.mark.asyncio
async def test_costs_summary_scoped_to_workspace():
    """A user that is NOT a member gets 403."""
    sm = await get_sessionmaker_for_tests()
    owner = await _make_user(sm)
    outsider = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, owner)
    await _seed_cost_events(sm, ws.id, owner.id)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: outsider

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/costs/summary?workspace_id={ws.id}")

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_costs_events_returns_rows():
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, user)
    await _seed_cost_events(sm, ws.id, user.id)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/costs/events?workspace_id={ws.id}")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) >= 2
    assert all(i["workspace_id"] == str(ws.id) for i in items)


@pytest.mark.asyncio
async def test_costs_events_scoped_to_workspace():
    sm = await get_sessionmaker_for_tests()
    owner = await _make_user(sm)
    outsider = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, owner)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: outsider

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/costs/events?workspace_id={ws.id}")

    assert r.status_code == 403
