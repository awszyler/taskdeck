from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, TaskDependency, Workspace


async def _make_task(session, *, workspace_id):
    t = Task(
        workspace_id=workspace_id,
        title=f"t-{uuid4().hex[:6]}",
        prompt="x",
        origin="web",
        agent="shell",
        status="pending",
    )
    session.add(t)
    await session.flush()
    return t


@pytest.mark.asyncio
async def test_task_dependency_insert_and_query():
    sm = await get_sessionmaker_for_tests()
    async with sm() as s:
        ws = Workspace(slug=f"dep-{uuid4().hex[:6]}", name="dep-test")
        s.add(ws)
        await s.commit()
        parent = await _make_task(s, workspace_id=ws.id)
        child = await _make_task(s, workspace_id=ws.id)
        dep = TaskDependency(
            parent_task_id=parent.id,
            child_task_id=child.id,
            created_at=datetime.now(UTC),
        )
        s.add(dep)
        await s.commit()

        rows = (
            await s.scalars(
                select(TaskDependency).where(TaskDependency.child_task_id == child.id)
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].parent_task_id == parent.id


@pytest.mark.asyncio
async def test_self_dependency_rejected():
    sm = await get_sessionmaker_for_tests()
    async with sm() as s:
        ws = Workspace(slug=f"dep-s-{uuid4().hex[:6]}", name="self")
        s.add(ws)
        await s.commit()
        t = await _make_task(s, workspace_id=ws.id)
        s.add(TaskDependency(
            parent_task_id=t.id, child_task_id=t.id, created_at=datetime.now(UTC),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_duplicate_dependency_rejected():
    sm = await get_sessionmaker_for_tests()
    async with sm() as s:
        ws = Workspace(slug=f"dep-d-{uuid4().hex[:6]}", name="dup")
        s.add(ws)
        await s.commit()
        parent = await _make_task(s, workspace_id=ws.id)
        child = await _make_task(s, workspace_id=ws.id)
        s.add(TaskDependency(
            parent_task_id=parent.id, child_task_id=child.id, created_at=datetime.now(UTC),
        ))
        await s.commit()

        s.add(TaskDependency(
            parent_task_id=parent.id, child_task_id=child.id, created_at=datetime.now(UTC),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_multi_parent_allowed():
    sm = await get_sessionmaker_for_tests()
    async with sm() as s:
        ws = Workspace(slug=f"dep-m-{uuid4().hex[:6]}", name="multi")
        s.add(ws)
        await s.commit()
        p1 = await _make_task(s, workspace_id=ws.id)
        p2 = await _make_task(s, workspace_id=ws.id)
        child = await _make_task(s, workspace_id=ws.id)
        s.add_all([
            TaskDependency(parent_task_id=p1.id, child_task_id=child.id, created_at=datetime.now(UTC)),
            TaskDependency(parent_task_id=p2.id, child_task_id=child.id, created_at=datetime.now(UTC)),
        ])
        await s.commit()

        parents = (await s.scalars(
            select(TaskDependency).where(TaskDependency.child_task_id == child.id)
        )).all()
        assert len(parents) == 2
