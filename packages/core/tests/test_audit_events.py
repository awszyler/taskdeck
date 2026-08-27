"""Tests for AuditEventSink — fire each kind and assert DB rows."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from taskdeck_core.audit.sink import AuditEventSink
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import AuditEvent, User, Workspace


async def _make_user(sm) -> User:
    async with sm() as s:
        u = User(
            workspace_id=None,
            email=f"{uuid4().hex[:8]}@test.com",
            name="Audit Sink User",
            role="member",
            cognito_sub=uuid4().hex,
            login=uuid4().hex[:8],
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


async def _make_workspace(sm) -> Workspace:
    async with sm() as s:
        ws = Workspace(slug=f"aud-{uuid4().hex[:8]}", name="audit-test")
        s.add(ws)
        await s.commit()
        await s.refresh(ws)
        return ws


async def _cleanup(sm, *workspace_ids) -> None:
    async with sm() as s:
        for wid in workspace_ids:
            ws = await s.get(Workspace, wid)
            if ws:
                await s.delete(ws)
        await s.commit()


@pytest.mark.asyncio
async def test_login_event_persisted():
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    sink = AuditEventSink(sessionmaker=sm)
    event = {
        "type": "audit.event",
        "kind": "login",
        "user_id": str(user.id),
        "meta": {"provider": "github"},
    }
    await sink.handle(event)

    async with sm() as s:
        rows = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.kind == "login", AuditEvent.user_id == user.id)
                .order_by(AuditEvent.created_at.desc())
                .limit(1)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].kind == "login"
    assert rows[0].meta == {"provider": "github"}
    assert rows[0].workspace_id is None

    # Cleanup
    async with sm() as s:
        row = await s.get(AuditEvent, rows[0].id)
        if row:
            await s.delete(row)
        u = await s.get(User, user.id)
        if u:
            await s.delete(u)
        await s.commit()


@pytest.mark.asyncio
async def test_workspace_create_event_persisted():
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace(sm)
    try:
        sink = AuditEventSink(sessionmaker=sm)
        event = {
            "type": "audit.event",
            "kind": "workspace.create",
            "user_id": str(user.id),
            "workspace_id": str(ws.id),
            "target_type": "workspace",
            "target_id": str(ws.id),
            "meta": {"slug": ws.slug, "name": ws.name},
        }
        await sink.handle(event)

        async with sm() as s:
            rows = (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.workspace_id == ws.id)
                )
            ).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.kind == "workspace.create"
        assert row.target_type == "workspace"
        assert row.target_id == ws.id
        assert row.meta["slug"] == ws.slug
    finally:
        await _cleanup(sm, ws.id)
        async with sm() as s:
            u = await s.get(User, user.id)
            if u:
                await s.delete(u)
            await s.commit()


@pytest.mark.asyncio
async def test_invite_issue_event_persisted():
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace(sm)
    try:
        sink = AuditEventSink(sessionmaker=sm)
        event = {
            "type": "audit.event",
            "kind": "invite.issue",
            "user_id": str(user.id),
            "workspace_id": str(ws.id),
            "target_type": "invite",
            "meta": {"code_prefix": "abcd"},
        }
        await sink.handle(event)

        async with sm() as s:
            rows = (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.workspace_id == ws.id)
                )
            ).scalars().all()

        assert len(rows) == 1
        assert rows[0].kind == "invite.issue"
        assert rows[0].target_type == "invite"
        assert rows[0].meta == {"code_prefix": "abcd"}
    finally:
        await _cleanup(sm, ws.id)
        async with sm() as s:
            u = await s.get(User, user.id)
            if u:
                await s.delete(u)
            await s.commit()


@pytest.mark.asyncio
async def test_member_remove_event_persisted():
    sm = await get_sessionmaker_for_tests()
    actor = await _make_user(sm)
    target = await _make_user(sm)
    ws = await _make_workspace(sm)
    try:
        sink = AuditEventSink(sessionmaker=sm)
        event = {
            "type": "audit.event",
            "kind": "member.remove",
            "user_id": str(actor.id),
            "workspace_id": str(ws.id),
            "target_type": "user",
            "target_id": str(target.id),
        }
        await sink.handle(event)

        async with sm() as s:
            rows = (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.workspace_id == ws.id)
                )
            ).scalars().all()

        assert len(rows) == 1
        assert rows[0].kind == "member.remove"
        assert rows[0].target_type == "user"
        assert rows[0].target_id == target.id
    finally:
        await _cleanup(sm, ws.id)
        async with sm() as s:
            for uid in (actor.id, target.id):
                u = await s.get(User, uid)
                if u:
                    await s.delete(u)
            await s.commit()


@pytest.mark.asyncio
async def test_non_audit_event_type_ignored():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        sink = AuditEventSink(sessionmaker=sm)
        await sink.handle({"type": "cost.event", "kind": "login", "workspace_id": str(ws.id)})

        async with sm() as s:
            rows = (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.workspace_id == ws.id)
                )
            ).scalars().all()

        assert len(rows) == 0
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_invalid_uuid_dropped():
    sm = await get_sessionmaker_for_tests()
    sink = AuditEventSink(sessionmaker=sm)
    # Should not raise, should just drop
    await sink.handle({
        "type": "audit.event",
        "kind": "login",
        "user_id": "not-a-uuid",
    })
    # No assertion needed — just verifying no exception
