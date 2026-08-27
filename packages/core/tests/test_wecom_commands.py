from __future__ import annotations

from uuid import uuid4

import pytest
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import ImIdentityLink, Task, User, Workspace  # noqa: F401
from taskdeck_core.im.wecom.binder import BindCodeCache
from taskdeck_core.im.wecom.commands import (
    handle_bind,
    handle_cancel,
    handle_free_text,
    handle_status,
)


async def _seed(sm, *, ws_slug: str) -> tuple[Workspace, list[Task]]:
    async with sm() as s:
        ws = Workspace(slug=ws_slug, name=ws_slug)
        s.add(ws)
        await s.commit()
        tasks = []
        for i in range(3):
            t = Task(
                workspace_id=ws.id,
                title=f"task {i}",
                prompt="x",
                origin="web",
                agent="shell",
                status="pending" if i == 0 else "done",
            )
            s.add(t)
            tasks.append(t)
        await s.commit()
        for t in tasks:
            await s.refresh(t)
        await s.refresh(ws)
        return ws, tasks


@pytest.mark.asyncio
async def test_handle_bind_creates_link():
    sm = await get_sessionmaker_for_tests()
    ws, _ = await _seed(sm, ws_slug=f"b-{uuid4().hex[:6]}")
    cache = BindCodeCache()
    code, _ = cache.issue(workspace_id=ws.id)

    async with sm() as s:
        reply = await handle_bind(code=code, external_id="UserX", session=s, cache=cache)

    assert "✓" in reply or "Bound" in reply
    # Link should be present now
    async with sm() as s:
        from sqlalchemy import select
        link = (await s.scalars(
            select(ImIdentityLink).where(ImIdentityLink.external_id == "UserX")
        )).first()
        assert link is not None
        assert link.workspace_id == ws.id


@pytest.mark.asyncio
async def test_handle_bind_invalid_code():
    sm = await get_sessionmaker_for_tests()
    cache = BindCodeCache()
    async with sm() as s:
        reply = await handle_bind(code="NOPE99", external_id="UserX", session=s, cache=cache)
    assert "❌" in reply or "invalid" in reply.lower()


@pytest.mark.asyncio
async def test_handle_status_before_bind():
    sm = await get_sessionmaker_for_tests()
    async with sm() as s:
        reply = await handle_status(external_id="UnboundUser", session=s)
    assert "not bound" in reply.lower()


@pytest.mark.asyncio
async def test_handle_status_after_bind_lists_tasks():
    sm = await get_sessionmaker_for_tests()
    ws, _ = await _seed(sm, ws_slug=f"st-{uuid4().hex[:6]}")
    cache = BindCodeCache()
    code, _ = cache.issue(workspace_id=ws.id)
    async with sm() as s:
        await handle_bind(code=code, external_id="UserB", session=s, cache=cache)
    async with sm() as s:
        reply = await handle_status(external_id="UserB", session=s)
    assert "task " in reply  # at least one of our seeded tasks
    assert reply.count("\n") == 2  # 3 tasks, 2 newlines


@pytest.mark.asyncio
async def test_handle_cancel_transitions_pending_to_cancelled():
    sm = await get_sessionmaker_for_tests()
    ws, tasks = await _seed(sm, ws_slug=f"cn-{uuid4().hex[:6]}")
    cache = BindCodeCache()
    code, _ = cache.issue(workspace_id=ws.id)
    async with sm() as s:
        await handle_bind(code=code, external_id="UserC", session=s, cache=cache)

    pending_task = tasks[0]  # seeded in "pending"
    prefix = str(pending_task.id)[:8]

    async with sm() as s:
        reply = await handle_cancel(target=prefix, external_id="UserC", session=s)
    assert "✓" in reply or "cancelled" in reply.lower()

    async with sm() as s:
        from sqlalchemy import select
        t = (await s.scalars(
            select(Task).where(Task.id == pending_task.id)
        )).first()
        assert t is not None
        assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_handle_cancel_ambiguous_prefix():
    sm = await get_sessionmaker_for_tests()
    ws, tasks = await _seed(sm, ws_slug=f"cam-{uuid4().hex[:6]}")
    cache = BindCodeCache()
    code, _ = cache.issue(workspace_id=ws.id)
    async with sm() as s:
        await handle_bind(code=code, external_id="UserD", session=s, cache=cache)

    async with sm() as s:
        reply = await handle_cancel(target="", external_id="UserD", session=s)
    assert "Usage" in reply or "❌" in reply


@pytest.mark.asyncio
async def test_handle_free_text_creates_pending_task():
    sm = await get_sessionmaker_for_tests()
    ws, _ = await _seed(sm, ws_slug=f"ft-{uuid4().hex[:6]}")
    cache = BindCodeCache()
    code, _ = cache.issue(workspace_id=ws.id)
    ext_id = f"UserE-{uuid4().hex[:6]}"
    async with sm() as s:
        await handle_bind(code=code, external_id=ext_id, session=s, cache=cache)

    from taskdeck_core.intent.schema import ParsedIntent

    class _StaticParser:
        async def parse(self, _input, **_kwargs):
            return ParsedIntent(
                title="Echo hi",
                agent="shell",
                prompt="echo hi",
                confidence=0.9,
            )

    class _NoopDispatcher:
        async def try_dispatch_pending(self, _session):
            return 0

    async with sm() as s:
        reply = await handle_free_text(
            content="please echo hi",
            external_id=ext_id,
            session=s,
            parser=_StaticParser(),
            public_base_url="http://localhost",
            sessionmaker=sm,
            dispatcher=_NoopDispatcher(),
        )
    assert "Task #" in reply or "task #" in reply.lower()
    assert "Echo hi" in reply

    # Task row exists
    async with sm() as s:
        from sqlalchemy import select as sa_select
        t = (await s.scalars(
            sa_select(Task).where(Task.workspace_id == ws.id, Task.origin == "im")
        )).first()
        assert t is not None
        assert t.status == "pending"
        assert t.raw_input == "please echo hi"


@pytest.mark.asyncio
async def test_handle_free_text_not_bound():
    sm = await get_sessionmaker_for_tests()

    class _ShouldNotRunParser:
        async def parse(self, _input, **_kwargs):
            raise AssertionError("parser must not be called when user isn't bound")

    class _NoopDispatcher:
        async def try_dispatch_pending(self, _session):
            return 0

    async with sm() as s:
        reply = await handle_free_text(
            content="anything",
            external_id="NobodyZ",
            session=s,
            parser=_ShouldNotRunParser(),
            public_base_url="http://h",
            sessionmaker=sm,
            dispatcher=_NoopDispatcher(),
        )
    assert "bound" in reply.lower()
