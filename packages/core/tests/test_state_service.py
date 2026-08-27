from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from taskdeck_core.db.models import Task, TaskEvent
from taskdeck_core.state.machine import IllegalTransition, TaskStatus
from taskdeck_core.state.service import TaskStateService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_transition_writes_event_and_updates_status(
    session: AsyncSession, draft_task: Task
):
    svc = TaskStateService(session)
    await svc.transition(
        draft_task.id, TaskStatus.PENDING, actor="user", reason="submit"
    )

    await session.refresh(draft_task)
    assert draft_task.status == TaskStatus.PENDING.value

    events = (
        await session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == draft_task.id)
        )
    ).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.from_status == TaskStatus.PARSE_FAILED.value
    assert ev.to_status == TaskStatus.PENDING.value
    assert ev.actor == "user"
    assert ev.reason == "submit"


async def test_illegal_transition_raises(session: AsyncSession, draft_task: Task):
    svc = TaskStateService(session)
    with pytest.raises(IllegalTransition):
        await svc.transition(draft_task.id, TaskStatus.RUNNING, actor="user")

    await session.refresh(draft_task)
    assert draft_task.status == TaskStatus.PARSE_FAILED.value


# ---------------------------------------------------------------------------
# admin_transition tests
# ---------------------------------------------------------------------------


async def _make_done_task(session: AsyncSession, draft_task: Task) -> Task:
    """Helper: advance draft_task to done via legal path."""
    svc = TaskStateService(session)
    await svc.transition(draft_task.id, TaskStatus.PENDING, actor="user")
    await svc.transition(draft_task.id, TaskStatus.RUNNING, actor="runner")
    await svc.transition(draft_task.id, TaskStatus.DONE, actor="runner")
    await session.refresh(draft_task)
    return draft_task


async def test_admin_transition_bypasses_legal(session: AsyncSession, draft_task: Task):
    """admin_transition can move done → pending which is illegal under user path."""
    done_task = await _make_done_task(session, draft_task)
    assert done_task.status == TaskStatus.DONE.value

    svc = TaskStateService(session)
    # done → pending is illegal via transition(), but admin_transition should succeed.
    await svc.admin_transition(done_task.id, TaskStatus.PENDING, actor="admin")

    await session.refresh(done_task)
    assert done_task.status == TaskStatus.PENDING.value


async def test_admin_transition_resets_finished_at(session: AsyncSession, draft_task: Task):
    """When admin moves a task back from a terminal state, finished_at is cleared."""
    done_task = await _make_done_task(session, draft_task)
    assert done_task.finished_at is not None

    svc = TaskStateService(session)
    await svc.admin_transition(done_task.id, TaskStatus.PENDING, actor="admin")

    await session.refresh(done_task)
    assert done_task.finished_at is None


async def test_admin_transition_writes_event_with_admin_actor(
    session: AsyncSession, draft_task: Task
):
    """admin_transition writes a TaskEvent row with actor='admin'."""
    svc = TaskStateService(session)
    await svc.admin_transition(
        draft_task.id, TaskStatus.CANCELLED, actor="admin", reason="test_override"
    )

    events = (
        await session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == draft_task.id)
        )
    ).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.actor == "admin"
    assert ev.from_status == TaskStatus.PARSE_FAILED.value
    assert ev.to_status == TaskStatus.CANCELLED.value
    assert ev.reason == "test_override"


async def test_admin_transition_publishes_to_bus(session: AsyncSession, draft_task: Task):
    """admin_transition calls publisher.publish when a publisher is set."""
    mock_publisher = AsyncMock()
    mock_publisher.publish = AsyncMock()

    svc = TaskStateService(session, publisher=mock_publisher)
    await svc.admin_transition(draft_task.id, TaskStatus.CANCELLED, actor="admin")

    mock_publisher.publish.assert_called_once()
    payload = mock_publisher.publish.call_args[0][0]
    assert payload["type"] == "task.event"
    assert payload["from"] == TaskStatus.PARSE_FAILED.value
    assert payload["to"] == TaskStatus.CANCELLED.value
    assert payload["actor"] == "admin"


async def test_admin_transition_sets_started_at_when_running(
    session: AsyncSession, draft_task: Task
):
    """admin_transition sets started_at when target is RUNNING."""
    svc = TaskStateService(session)
    await svc.admin_transition(draft_task.id, TaskStatus.RUNNING, actor="admin")

    await session.refresh(draft_task)
    assert draft_task.started_at is not None
    assert draft_task.status == TaskStatus.RUNNING.value


async def test_admin_transition_raises_on_missing_task(session: AsyncSession):
    """admin_transition raises LookupError for unknown task IDs."""
    from uuid import uuid4

    svc = TaskStateService(session)
    with pytest.raises(LookupError):
        await svc.admin_transition(uuid4(), TaskStatus.PENDING, actor="admin")
