from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from taskdeck_core.db.models import Task, TaskEvent
from taskdeck_core.metrics.registry import TASK_STATE_TRANSITIONS_TOTAL

from .machine import TaskStatus, next_status

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


class EventPublisher(Protocol):
    async def publish(self, event: dict) -> None: ...


class TaskStateService:
    def __init__(
        self, session: AsyncSession, publisher: EventPublisher | None = None
    ):
        self._session = session
        self._publisher = publisher

    def _apply_status_side_effects(self, task: Task, new: TaskStatus, now: datetime) -> None:
        """Apply timestamp side-effects based on the new status."""
        if new is TaskStatus.RUNNING:
            task.started_at = now
        elif new in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            task.finished_at = now
        elif new in {TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.PARSE_FAILED}:
            # Reset finished_at when moving back from a terminal state.
            task.finished_at = None

    async def transition(
        self,
        task_id: UUID,
        target: TaskStatus,
        *,
        actor: str,
        actor_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        task = await self._session.get(Task, task_id, with_for_update=True)
        if task is None:
            raise LookupError(f"task {task_id} not found")

        current = TaskStatus(task.status)
        new = next_status(current, target)

        task.status = new.value
        now = datetime.now(UTC)
        self._apply_status_side_effects(task, new, now)

        event = TaskEvent(
            task_id=task_id,
            from_status=current.value,
            to_status=new.value,
            actor=actor,
            actor_id=actor_id,
            reason=reason,
            created_at=now,
        )
        self._session.add(event)
        await self._session.flush()

        TASK_STATE_TRANSITIONS_TOTAL.labels(
            from_status=current.value, to_status=new.value, actor=actor
        ).inc()

        if self._publisher is not None:
            await self._publisher.publish(
                {
                    "type": "task.event",
                    "task_id": str(task_id),
                    "from": current.value,
                    "to": new.value,
                    "actor": actor,
                    "at": now.isoformat(),
                }
            )

    async def admin_transition(
        self,
        task_id: UUID,
        target: TaskStatus,
        *,
        actor: str = "admin",
        actor_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Transition without enforcing _LEGAL. For operator overrides only."""
        task = await self._session.get(Task, task_id, with_for_update=True)
        if task is None:
            raise LookupError(f"task {task_id} not found")

        current = TaskStatus(task.status)
        new = target

        task.status = new.value
        now = datetime.now(UTC)
        self._apply_status_side_effects(task, new, now)

        event = TaskEvent(
            task_id=task_id,
            from_status=current.value,
            to_status=new.value,
            actor=actor,
            actor_id=actor_id,
            reason=reason or "admin_override",
            created_at=now,
        )
        self._session.add(event)
        await self._session.flush()

        TASK_STATE_TRANSITIONS_TOTAL.labels(
            from_status=current.value, to_status=new.value, actor=actor
        ).inc()

        if self._publisher is not None:
            await self._publisher.publish(
                {
                    "type": "task.event",
                    "task_id": str(task_id),
                    "from": current.value,
                    "to": new.value,
                    "actor": actor,
                    "at": now.isoformat(),
                }
            )
