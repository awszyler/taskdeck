"""Async intent parse runner.

Glue that runs `IntentParseLoop` in the background after a raw-input task
is created in PARSING state, then transitions the task to PENDING. As of
the parsing-UX rework (docs/parsing-ux-rework.md) a successful parse ALWAYS
goes straight to PENDING — confidence is recorded but no longer gates a
DRAFT review step (the user never edited those). The only non-PENDING
outcome is PARSE_FAILED, used when the parser itself can't produce a usable
spec; that surfaces in the "Needs you" column for edit-&-resubmit.

Why this lives here: the loop itself doesn't know about Tasks, EventBus, or
sessions. This module owns that wiring so the loop stays unit-testable.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from taskdeck_core.db.models import IntentParseLog, Task
from taskdeck_core.intent.schema import IntentContext, IntentInput
from taskdeck_core.state.machine import TaskStatus
from taskdeck_core.state.service import TaskStateService

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from taskdeck_core.api.ws import EventBus
    from taskdeck_core.crp.hub import RunnerHub
    from taskdeck_core.dispatcher.service import Dispatcher
    from taskdeck_core.intent.loop import IntentParseLoop, ParseOutcome

log = logging.getLogger(__name__)


async def run_parse_loop(
    *,
    task_id: UUID,
    raw_input: str,
    sessionmaker: async_sessionmaker,
    loop: IntentParseLoop,
    hub: RunnerHub | None,
    bus: EventBus | None,
    dispatcher: Dispatcher | None,
    hint: Literal["voice", "text", "im"] = "text",
) -> None:
    """Background loop runner. Idempotent on outcome (it transitions the task
    via the state machine; second invocations would raise IllegalTransition,
    so callers must guarantee single dispatch — `asyncio.create_task` does)."""
    try:
        # Capability snapshot taken at parse time.
        caps: list[dict[str, str]] = []
        if hub is not None:
            caps = hub.available_capabilities()

        # P7: pull attachment filenames in a short read-only session so the
        # parser can see what the user uploaded. Without this, prompts like
        # "总结一下文件" look ambiguous (no referent in raw_input). Less
        # critical now that confidence doesn't gate, but still improves the
        # agent routing and the generated title/description.
        attachment_names: list[str] = []
        async with sessionmaker() as ds:
            from sqlalchemy import select as sa_select

            from taskdeck_core.db.models import TaskAttachment
            rows = (await ds.scalars(
                sa_select(TaskAttachment.original_filename)
                .where(TaskAttachment.task_id == task_id)
            )).all()
            attachment_names = list(rows)

        outcome: ParseOutcome = await loop.run(
            IntentInput(
                raw_input=raw_input,
                hint=hint,
                context=IntentContext(),
                attachments=attachment_names,
            ),
            capabilities=caps,
        )

        async with sessionmaker() as session:
            task = await session.get(Task, task_id)
            if task is None:
                log.warning("parse loop: task %s vanished mid-flight", task_id)
                return

            # Record the parse for cost / debug.
            session.add(IntentParseLog(
                task_id=task.id,
                raw_input=raw_input,
                parsed_output=outcome.parsed.model_dump(mode="json"),
                model=outcome.last_model or "heuristic",
                latency_ms=0,  # loop doesn't track wall-time per attempt centrally yet
                success=outcome.result != "heuristic" and outcome.parsed.confidence > 0,
                created_at=datetime.now(UTC),
            ))

            # Hydrate task with parser output.
            task.title = outcome.parsed.title
            task.description = outcome.parsed.description
            task.prompt = outcome.parsed.prompt
            task.agent = outcome.parsed.agent
            task.repo = outcome.parsed.repo
            task.base_branch = outcome.parsed.base_branch or "main"
            task.intent_confidence = outcome.parsed.confidence
            await session.flush()

            # Any successful parse goes to the queue. Confidence is recorded
            # above but no longer routes to a review step.
            target = TaskStatus.PENDING

            # If the task has unsatisfied dependencies, hold it in BLOCKED
            # instead of PENDING so DependencyResolver can flip it to PENDING
            # when the last parent finishes. Without this, a child created
            # via the raw-input async path would race past its parents and
            # dispatch immediately with no upstream context.
            from sqlalchemy import select as sa_select

            from taskdeck_core.db.models import TaskDependency
            parent_statuses = (await session.scalars(
                sa_select(Task.status)
                .join(TaskDependency, TaskDependency.parent_task_id == Task.id)
                .where(TaskDependency.child_task_id == task.id)
            )).all()
            if parent_statuses and not all(
                s == TaskStatus.DONE.value for s in parent_statuses
            ):
                target = TaskStatus.BLOCKED
                log.info(
                    "parse loop: task %s has unsatisfied deps, "
                    "transitioning to BLOCKED instead of PENDING",
                    task.id,
                )

            svc = TaskStateService(session, publisher=bus)
            await svc.transition(
                task.id,
                target,
                actor="system",
                reason=f"parse loop: {outcome.result} (attempts={outcome.attempts})",
            )
            await session.commit()

        # Bus event for UI: task.parsed carries the final classification so
        # the kanban can swap the skeleton card for the real one.
        if bus is not None:
            await bus.publish({
                "type": "task.parsed",
                "task_id": str(task_id),
                "status": target.value,
                "agent": outcome.parsed.agent,
                "confidence": outcome.parsed.confidence,
                "result": outcome.result,
                "attempts": outcome.attempts,
            })

        # Auto-submit kicks the dispatcher just like a regular submit would.
        if target is TaskStatus.PENDING and dispatcher is not None:
            async with sessionmaker() as ds:
                await dispatcher.try_dispatch_pending(ds)
    except Exception as exc:  # noqa: BLE001
        # Last-ditch safety net: if anything in the runner blew up, mark the
        # task PARSE_FAILED so the user sees it in "Needs you" and can edit &
        # resubmit or cancel. We can't let parsing tasks linger forever.
        log.exception("parse loop crashed for task %s; -> parse_failed", task_id)
        try:
            async with sessionmaker() as session:
                task = await session.get(Task, task_id)
                if task is not None and task.status == TaskStatus.PARSING.value:
                    # Surface the error in summary so the drawer can show why.
                    task.summary = f"Parsing failed: {exc}"[:500]
                    svc = TaskStateService(session, publisher=bus)
                    await svc.transition(
                        task.id,
                        TaskStatus.PARSE_FAILED,
                        actor="system",
                        reason="parse loop crashed",
                    )
                    await session.commit()
            if bus is not None:
                await bus.publish({
                    "type": "task.parsed",
                    "task_id": str(task_id),
                    "status": TaskStatus.PARSE_FAILED.value,
                })
        except Exception:  # noqa: BLE001
            log.exception("failed to mark crashed parsing task %s", task_id)
