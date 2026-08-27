from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from taskdeck_core.db.models import Task, TaskDependency
from taskdeck_core.state.machine import TERMINAL, IllegalTransition, TaskStatus
from taskdeck_core.state.service import TaskStateService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

log = logging.getLogger(__name__)


class DependencyResolver:
    """Reacts to task terminal events by advancing blocked children.

    On parent.done: if all of the child's parents are done, move the child
    from blocked -> pending (and kick the dispatcher).
    On parent.failed or parent.cancelled: cascade cancel children that
    haven't started yet (draft | blocked | pending).
    """

    def __init__(self, sessionmaker: async_sessionmaker, dispatcher, bus=None):
        self._sm = sessionmaker
        self._dispatcher = dispatcher
        self._bus = bus

    async def handle(self, event: dict) -> None:
        if event.get("type") != "task.event":
            return
        to_status = event.get("to")
        if to_status not in {s.value for s in TERMINAL}:
            return
        try:
            parent_id = UUID(event["task_id"])
        except (KeyError, ValueError):
            return

        affected = False
        async with self._sm() as session:
            child_rows = await self._fetch_children(session, parent_id)
            for child in child_rows:
                if to_status == TaskStatus.DONE.value:
                    if (
                        child.status == TaskStatus.BLOCKED.value
                        and await self._all_parents_done(session, child.id)
                    ):
                        await self._safe_transition(
                            session, child.id, TaskStatus.PENDING,
                            reason="deps_satisfied",
                        )
                        affected = True
                else:
                    # failed or cancelled
                    if child.status in {
                        TaskStatus.PARSE_FAILED.value,
                        TaskStatus.BLOCKED.value,
                        TaskStatus.PENDING.value,
                    }:
                        await self._safe_transition(
                            session, child.id, TaskStatus.CANCELLED,
                            reason=f"dependency_{to_status}",
                        )
            await session.commit()

        if affected and self._dispatcher is not None:
            async with self._sm() as dispatch_sess:
                await self._dispatcher.try_dispatch_pending(dispatch_sess)

    async def _fetch_children(self, session, parent_id):
        stmt = (
            select(Task)
            .join(TaskDependency, TaskDependency.child_task_id == Task.id)
            .where(TaskDependency.parent_task_id == parent_id)
        )
        return list((await session.scalars(stmt)).all())

    async def _all_parents_done(self, session, child_id) -> bool:
        stmt = (
            select(Task.status)
            .join(TaskDependency, TaskDependency.parent_task_id == Task.id)
            .where(TaskDependency.child_task_id == child_id)
        )
        statuses = (await session.scalars(stmt)).all()
        return all(s == TaskStatus.DONE.value for s in statuses) and len(statuses) > 0

    async def _safe_transition(self, session, task_id, target, *, reason):
        svc = TaskStateService(session, publisher=self._bus)
        try:
            await svc.transition(
                task_id, target, actor="system", actor_id="resolver", reason=reason,
            )
        except IllegalTransition as e:
            log.warning("resolver: illegal transition %s: %s", task_id, e)
