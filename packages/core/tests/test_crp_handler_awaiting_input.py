"""Tests for CRP handler: TaskAwaitingInput message."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from taskdeck_core.crp.handler import _handle_runner_message
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, TaskTurn, Workspace
from taskdeck_proto.crp import TaskAwaitingInput


class _StubConn:
    def __init__(self):
        self.runner_id = "test-runner"
        self._inflight = 1

    def decrement_inflight(self):
        self._inflight -= 1


@pytest.mark.asyncio
async def test_handle_task_awaiting_input_transitions_state_and_writes_turn():
    sm = await get_sessionmaker_for_tests()
    async with sm() as sess:
        ws = Workspace(slug=f"ws-{uuid4().hex[:8]}", name="test")
        sess.add(ws)
        await sess.flush()
        task = Task(
            workspace_id=ws.id, title="t", prompt="p", origin="web",
            agent="claude-code", status="running",
        )
        sess.add(task)
        await sess.commit()
        await sess.refresh(task)

    msg = TaskAwaitingInput(task_id=str(task.id), question="May I read README?")
    conn = _StubConn()
    dispatcher = MagicMock()
    dispatcher.try_dispatch_pending = AsyncMock(return_value=0)

    await _handle_runner_message(msg, conn, sm, dispatcher, bus=None)

    async with sm() as sess:
        refreshed = await sess.get(Task, task.id)
        assert refreshed is not None
        assert refreshed.status == "awaiting_input"
        turns = (
            await sess.scalars(
                select(TaskTurn).where(TaskTurn.task_id == task.id).order_by(TaskTurn.seq.asc())
            )
        ).all()
        assert len(turns) == 1
        assert turns[0].role == "agent"
        assert turns[0].content == "May I read README?"
        assert turns[0].seq == 0


@pytest.mark.asyncio
async def test_handle_task_awaiting_input_on_terminal_drops_silently():
    """If the task is already cancelled/done by the time the message arrives,
    the handler logs and drops without crashing."""
    sm = await get_sessionmaker_for_tests()
    async with sm() as sess:
        ws = Workspace(slug=f"ws-{uuid4().hex[:8]}", name="test")
        sess.add(ws)
        await sess.flush()
        task = Task(
            workspace_id=ws.id, title="t", prompt="p", origin="web",
            agent="claude-code", status="cancelled",
        )
        sess.add(task)
        await sess.commit()
        await sess.refresh(task)

    msg = TaskAwaitingInput(task_id=str(task.id), question="late?")
    conn = _StubConn()
    dispatcher = MagicMock()
    dispatcher.try_dispatch_pending = AsyncMock(return_value=0)

    # Should not raise.
    await _handle_runner_message(msg, conn, sm, dispatcher, bus=None)

    async with sm() as sess:
        refreshed = await sess.get(Task, task.id)
        assert refreshed.status == "cancelled"  # unchanged
        turns = (
            await sess.scalars(select(TaskTurn).where(TaskTurn.task_id == task.id))
        ).all()
        assert len(turns) == 0  # no agent turn written
