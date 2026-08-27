from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, TaskDependency, Workspace
from taskdeck_core.deps.resolver import DependencyResolver


class _NoopDispatcher:
    calls = 0

    async def try_dispatch_pending(self, _sess):
        self.calls += 1
        return 0


async def _seed(sm, *, ws_slug, parent_states=("done", "done"), child_state="blocked"):
    async with sm() as s:
        ws = Workspace(slug=ws_slug, name=ws_slug)
        s.add(ws)
        await s.commit()
        parents = []
        for st in parent_states:
            p = Task(
                workspace_id=ws.id,
                title=f"p-{uuid4().hex[:6]}",
                prompt="x",
                origin="web",
                agent="shell",
                status=st,
            )
            s.add(p)
            parents.append(p)
        child = Task(
            workspace_id=ws.id,
            title="child",
            prompt="x",
            origin="web",
            agent="shell",
            status=child_state,
        )
        s.add(child)
        await s.commit()
        now = datetime.now(UTC)
        for p in parents:
            s.add(TaskDependency(
                parent_task_id=p.id, child_task_id=child.id, created_at=now,
            ))
        await s.commit()
        await s.refresh(child)
        for p in parents:
            await s.refresh(p)
        return ws, parents, child


@pytest.mark.asyncio
async def test_parent_done_all_done_advances_blocked_to_pending():
    sm = await get_sessionmaker_for_tests()
    ws, parents, child = await _seed(sm, ws_slug=f"r1-{uuid4().hex[:6]}",
                                      parent_states=("done", "done"))
    dispatcher = _NoopDispatcher()
    resolver = DependencyResolver(sm, dispatcher)
    # Simulate the event for the LAST parent completing
    await resolver.handle({
        "type": "task.event",
        "task_id": str(parents[1].id),
        "to": "done",
    })
    async with sm() as s:
        c = await s.get(Task, child.id)
        assert c.status == "pending"
    # dispatcher kicked because we advanced a task
    assert dispatcher.calls == 1


@pytest.mark.asyncio
async def test_parent_done_some_still_running_stays_blocked():
    sm = await get_sessionmaker_for_tests()
    # parent_states: one done, one still running
    ws, parents, child = await _seed(sm, ws_slug=f"r2-{uuid4().hex[:6]}",
                                      parent_states=("done", "running"))
    dispatcher = _NoopDispatcher()
    resolver = DependencyResolver(sm, dispatcher)
    await resolver.handle({
        "type": "task.event",
        "task_id": str(parents[0].id),
        "to": "done",
    })
    async with sm() as s:
        c = await s.get(Task, child.id)
        assert c.status == "blocked"
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_parent_failed_cascades_cancelled():
    sm = await get_sessionmaker_for_tests()
    ws, parents, child = await _seed(sm, ws_slug=f"r3-{uuid4().hex[:6]}",
                                      parent_states=("failed", "running"),
                                      child_state="blocked")
    dispatcher = _NoopDispatcher()
    resolver = DependencyResolver(sm, dispatcher)
    await resolver.handle({
        "type": "task.event",
        "task_id": str(parents[0].id),
        "to": "failed",
    })
    async with sm() as s:
        c = await s.get(Task, child.id)
        assert c.status == "cancelled"


@pytest.mark.asyncio
async def test_non_task_event_is_ignored():
    sm = await get_sessionmaker_for_tests()
    resolver = DependencyResolver(sm, _NoopDispatcher())
    # Should not raise
    await resolver.handle({"type": "runner.status", "runner_id": "x", "status": "online"})
    await resolver.handle({"type": "task.event", "task_id": str(uuid4()), "to": "running"})
