from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError
from taskdeck_proto.crp import (
    CRPMessage,
    Hello,
    TaskAwaitingInput,
    TaskFailed,
    TaskFinished,
    Welcome,
    parse_message,
)
from taskdeck_proto.crp import (
    TaskLog as TaskLogMsg,
)

from taskdeck_core.db.models import Task, TaskLog, TaskTurn
from taskdeck_core.settings import Settings
from taskdeck_core.state.machine import TaskStatus
from taskdeck_core.state.service import TaskStateService

from .hub import RunnerConnection, RunnerHub

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from taskdeck_core.dispatcher.service import Dispatcher

log = logging.getLogger(__name__)


def crp_router(
    hub: RunnerHub,
    dispatcher: Dispatcher,
    bus=None,  # EventBus | None (avoid circular import)
    app_ref=None,  # FastAPI app — used to lazily read app.state.db_sessionmaker
    settings: Settings | None = None,
) -> APIRouter:
    r = APIRouter()
    s = settings or Settings()  # type: ignore[call-arg]

    @r.websocket("/api/v1/crp/connect")
    async def endpoint(ws: WebSocket) -> None:
        auth = ws.headers.get("authorization", "")
        expected = f"Bearer {s.runner_bearer_token}"
        if auth != expected:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await ws.accept()

        # First frame must be hello
        try:
            first = await ws.receive_json()
            hello = parse_message(first)
        except (WebSocketDisconnect, ValidationError, ValueError):
            await ws.close(code=status.WS_1002_PROTOCOL_ERROR)
            return

        if not isinstance(hello, Hello):
            await ws.close(code=status.WS_1002_PROTOCOL_ERROR)
            return

        conn = RunnerConnection(
            runner_id=hello.runner_id,
            socket=ws,
            max_parallel=hello.max_parallel,
            capabilities=hello.capabilities,
            capability_descriptions=hello.capability_descriptions,
        )
        hub.register(conn)
        await ws.send_json(Welcome().model_dump())

        # Resolve sessionmaker lazily from app state (set by lifespan).
        sm: async_sessionmaker = ws.app.state.db_sessionmaker

        # Kick dispatcher — a new runner may unblock pending tasks.
        async with sm() as sess:
            await dispatcher.try_dispatch_pending(sess)

        try:
            while True:
                raw = await ws.receive_json()
                try:
                    msg = parse_message(raw)
                except ValidationError:
                    log.warning("bad CRP message from %s: %r", hello.runner_id, raw)
                    continue
                await _handle_runner_message(msg, conn, sm, dispatcher, bus)
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(hello.runner_id)
            # Any tasks that were running on this runner stay 'running' in DB;
            # orphan recovery marks them failed on next core restart.

    return r


async def _handle_runner_message(
    msg: CRPMessage,
    conn: RunnerConnection,
    sm: async_sessionmaker,
    dispatcher: Dispatcher,
    bus=None,  # EventBus | None
) -> None:
    if isinstance(msg, TaskLogMsg):
        async with sm() as sess:
            sess.add(
                TaskLog(
                    task_id=UUID(msg.task_id),
                    seq=msg.seq,
                    stream=msg.stream,
                    data=msg.data,
                    created_at=datetime.now(UTC),
                )
            )
            await sess.commit()
        return

    if isinstance(msg, TaskFinished):
        async with sm() as sess:
            svc = TaskStateService(sess, publisher=bus)
            await svc.transition(
                UUID(msg.task_id),
                TaskStatus.DONE,
                actor="runner",
                actor_id=conn.runner_id,
                reason=f"exit_code={msg.exit_code}",
            )
            task = await sess.get(Task, UUID(msg.task_id))
            if task is not None:
                task.exit_code = msg.exit_code
                if msg.summary:
                    task.summary = msg.summary
            await sess.commit()
        conn.decrement_inflight()
        async with sm() as sess:
            await dispatcher.try_dispatch_pending(sess)
        return

    if isinstance(msg, TaskAwaitingInput):
        from sqlalchemy import func
        from sqlalchemy import select as _select

        async with sm() as sess:
            task = await sess.get(Task, UUID(msg.task_id))
            if task is None:
                log.warning("awaiting_input for unknown task %s; dropping", msg.task_id)
                conn.decrement_inflight()
                return
            if task.status not in {"running", "awaiting_input"}:
                # Late-arriving message after cancel/done. Drop silently.
                log.info(
                    "awaiting_input for task %s in terminal state %s; dropping",
                    msg.task_id, task.status,
                )
                conn.decrement_inflight()
                return

            # Append agent turn at the next seq.
            next_seq = (
                await sess.execute(
                    _select(func.coalesce(func.max(TaskTurn.seq), -1) + 1)
                    .where(TaskTurn.task_id == task.id)
                )
            ).scalar_one()
            sess.add(TaskTurn(
                task_id=task.id, seq=next_seq, role="agent",
                content=msg.question, created_at=datetime.now(UTC),
            ))

            svc = TaskStateService(sess, publisher=bus)
            await svc.transition(
                UUID(msg.task_id),
                TaskStatus.AWAITING_INPUT,
                actor="runner",
                actor_id=conn.runner_id,
                reason="ccpt:ask",
            )
            await sess.commit()
        conn.decrement_inflight()
        return

    if isinstance(msg, TaskFailed):
        # Agent-level failure: route through IN_REVIEW so the user can
        # decide to retry or abandon. Preserve runner's `detail` in
        # task_events.reason for postmortem; also persist to task.summary
        # so it's visible on the review card without joining task_events.
        full_reason = msg.reason
        if msg.detail:
            full_reason = f"{msg.reason}: {msg.detail[:500]}"
        async with sm() as sess:
            svc = TaskStateService(sess, publisher=bus)
            await svc.transition(
                UUID(msg.task_id),
                TaskStatus.IN_REVIEW,
                actor="runner",
                actor_id=conn.runner_id,
                reason=full_reason,
            )
            task = await sess.get(Task, UUID(msg.task_id))
            if task is not None:
                task.exit_code = 1 if task.exit_code is None else task.exit_code
                if not task.summary:
                    task.summary = full_reason[:500]
            await sess.commit()
        conn.decrement_inflight()
        async with sm() as sess:
            await dispatcher.try_dispatch_pending(sess)
        return

    # Ignore other message types in M1 (ack, started) — purely informational.
