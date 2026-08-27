from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime  # noqa: TC003  — Pydantic resolves at runtime
from typing import Annotated
from uuid import UUID  # noqa: TCH003  — Pydantic resolves this type at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from taskdeck_core.auth.memberships import get_visible_workspace_ids
from taskdeck_core.auth.middleware import ServicePrincipal, current_principal, require_user
from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import (
    Task,
    TaskAttachment,
    TaskDependency,
    TaskLog,
    TaskTurn,
    User,
    WorkspaceMember,
)
from taskdeck_core.intent.async_runner import run_parse_loop
from taskdeck_core.intent.loop import IntentParseLoop
from taskdeck_core.state.machine import IllegalTransition, TaskStatus
from taskdeck_core.state.service import TaskStateService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

# Hold strong references to fire-and-forget background tasks so the
# asyncio event loop's weakref-based task tracker doesn't GC them
# mid-execution. Symptom of the bug this fixes: tasks getting stuck
# at PARSING forever because run_parse_loop disappeared as soon as
# the HTTP response returned. See:
#   https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro) -> asyncio.Task:
    """asyncio.create_task with a reference held until completion."""
    t = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t


class TaskCreateBody(BaseModel):
    """Two creation modes share this schema:

    1. Structured form (existing): caller already has agent/prompt/title.
       Pass `prompt` + `agent` (+ optional title, repo, etc.).

    2. Async raw-input form (new in P5.0): caller has only the raw user input.
       Pass `raw_input`. Server will create a parsing-state task and run the
       intent parse loop in the background.

    Validation: exactly one of (prompt, raw_input) must be set.
    """
    workspace_id: UUID
    # Structured-form fields. Optional now that raw-input mode exists.
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=280)
    prompt: str | None = None
    agent: str | None = None
    # Common metadata.
    origin: str = Field(default="web", pattern=r"^(web|voice|im|text)$")
    repo: str | None = None
    base_branch: str = "main"
    isolation: str = "worktree"
    timeout_seconds: int = 7200
    depends_on: list[UUID] = Field(default_factory=list, max_length=20)
    # P7 multimodal inputs. IDs returned by POST /attachments.
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    # Raw-input async path.
    raw_input: str | None = Field(default=None, max_length=8192)
    # Client-driven dedup. Caller generates a UUIDv4 per logical submit; double
    # POSTs (network retry, double-click) with the same key return the same task.
    idempotency_key: UUID | None = None


class TaskOut(BaseModel):
    id: UUID
    workspace_id: UUID
    title: str
    description: str | None = None
    prompt: str
    origin: str
    agent: str
    repo: str | None
    status: str
    assigned_runner_id: UUID | None
    exit_code: int | None
    summary: str | None
    created_at: datetime
    dependencies_count: int = 0
    raw_input: str | None = None
    intent_confidence: float | None = None
    # P6.3.7 cold archive — UI uses these to surface "Archived" + a
    # restoring spinner on the Open Sandbox menu item.
    archived_at: datetime | None = None
    # P7 multimodal — count of files the user attached to this task.
    # The card shows a paperclip when > 0; the drawer renders the
    # actual list via GET /tasks/{id}/attachments.
    attachments_count: int = 0

    @classmethod
    def from_model(
        cls,
        t: Task,
        *,
        dependencies_count: int = 0,
        attachments_count: int = 0,
    ) -> TaskOut:
        return cls(
            id=t.id,
            workspace_id=t.workspace_id,
            title=t.title,
            description=t.description,
            prompt=t.prompt,
            origin=t.origin,
            agent=t.agent,
            repo=t.repo,
            status=t.status,
            assigned_runner_id=t.assigned_runner_id,
            exit_code=t.exit_code,
            summary=t.summary,
            created_at=t.created_at,
            dependencies_count=dependencies_count,
            raw_input=t.raw_input,
            intent_confidence=t.intent_confidence,
            archived_at=t.archived_at,
            attachments_count=attachments_count,
        )


class TaskListOut(BaseModel):
    items: list[TaskOut]


class DepParentOut(BaseModel):
    id: UUID
    title: str
    status: str


class DependenciesOut(BaseModel):
    parents: list[DepParentOut]


class TaskLogEntry(BaseModel):
    seq: int
    stream: str  # "stdout" | "stderr"
    data: str
    created_at: datetime


class TaskLogsOut(BaseModel):
    items: list[TaskLogEntry]
    total: int
    returned: int
    truncated: bool


class TaskTurnOut(BaseModel):
    seq: int
    role: str  # "agent" | "user"
    content: str
    created_at: datetime


class TaskTurnsOut(BaseModel):
    items: list[TaskTurnOut]


SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[User | ServicePrincipal, Depends(current_principal)]


async def _attachment_counts(
    session: AsyncSession, task_ids: list[UUID],
) -> dict[UUID, int]:
    """Bulk count of attachments per task. Used by list_tasks so the
    kanban can show a paperclip without N+1 queries."""
    if not task_ids:
        return {}
    stmt = (
        select(TaskAttachment.task_id, func.count())
        .where(TaskAttachment.task_id.in_(task_ids))
        .group_by(TaskAttachment.task_id)
    )
    rows = (await session.execute(stmt)).all()
    return {tid: count for tid, count in rows if tid is not None}


async def _link_attachments(
    session: AsyncSession, *, task: Task, attachment_ids: list[UUID]
) -> None:
    """Bind previously-uploaded attachments to the just-created task.

    Validation:
    - all ids must exist
    - all must be in the same workspace as the task (cross-workspace
      reference would let an attacker dump another tenant's files into
      a task they own — refuse)
    - all must currently have task_id NULL (already-bound attachments
      can't be reassigned)

    Raises HTTPException on any failure; SQLAlchemy session rolls back
    in the caller's try/except.
    """
    if not attachment_ids:
        return
    unique_ids = list(set(attachment_ids))
    if len(unique_ids) != len(attachment_ids):
        raise HTTPException(422, "duplicate ids in attachment_ids")
    rows = (
        await session.scalars(
            select(TaskAttachment).where(TaskAttachment.id.in_(unique_ids))
        )
    ).all()
    if len(rows) != len(unique_ids):
        raise HTTPException(400, "one or more attachment_ids not found")
    for row in rows:
        if row.workspace_id != task.workspace_id:
            raise HTTPException(
                400,
                f"attachment {row.id} is in a different workspace",
            )
        if row.task_id is not None and row.task_id != task.id:
            raise HTTPException(
                409,
                f"attachment {row.id} is already linked to another task",
            )
        row.task_id = task.id


async def _validate_and_record_deps(
    session: AsyncSession, *, task: Task, depends_on: list[UUID]
) -> None:
    if not depends_on:
        return
    unique_ids = list(set(depends_on))
    if len(unique_ids) != len(depends_on):
        raise HTTPException(422, "duplicate ids in depends_on")
    parents = (
        await session.scalars(
            select(Task).where(Task.id.in_(unique_ids))
        )
    ).all()
    if len(parents) != len(unique_ids):
        raise HTTPException(400, "one or more depends_on IDs not found")
    for p in parents:
        if p.workspace_id != task.workspace_id:
            raise HTTPException(
                400, f"depends_on task {p.id} is in a different workspace"
            )
    now = datetime.now(UTC)
    for pid in unique_ids:
        session.add(
            TaskDependency(
                parent_task_id=pid,
                child_task_id=task.id,
                created_at=now,
            )
        )


async def _dep_counts(session: AsyncSession, task_ids: list[UUID]) -> dict[UUID, int]:
    if not task_ids:
        return {}
    stmt = (
        select(TaskDependency.child_task_id, func.count())
        .where(TaskDependency.child_task_id.in_(task_ids))
        .group_by(TaskDependency.child_task_id)
    )
    rows = (await session.execute(stmt)).all()
    return {task_id: count for task_id, count in rows}


async def _create_raw_input_task(
    *,
    body: TaskCreateBody,
    session: AsyncSession,
    request: Request,
) -> TaskOut:
    """Create a parsing-state task and kick off the intent parse loop in the
    background. Returns the parsing task immediately so the UI can show a
    skeleton card."""
    from sqlalchemy.exc import IntegrityError

    raw = body.raw_input or ""
    placeholder_title = (raw.strip()[:80] or "untitled")
    task = Task(
        workspace_id=body.workspace_id,
        title=placeholder_title,
        prompt=raw,  # placeholder; loop overwrites with the parsed prompt
        origin=body.origin,
        agent="",  # loop fills in
        repo=None,
        base_branch="main",
        isolation=body.isolation,
        timeout_seconds=body.timeout_seconds,
        status=TaskStatus.PARSING.value,
        idempotency_key=body.idempotency_key,
        raw_input=raw,
    )
    session.add(task)
    # Need the task.id before _validate_and_record_deps can insert
    # TaskDependency rows referencing it, but we don't want to commit
    # twice — flush gets us the PK in the same transaction. The
    # subsequent commit() persists task + deps atomically (or both
    # roll back on idempotency race).
    try:
        await session.flush()
        await _validate_and_record_deps(
            session, task=task, depends_on=body.depends_on,
        )
        await _link_attachments(
            session, task=task, attachment_ids=body.attachment_ids,
        )
        await session.commit()
    except IntegrityError:
        # Race: another concurrent POST with the same idempotency_key won the
        # unique-index check. Roll back and return the winner.
        await session.rollback()
        existing = await session.scalar(
            select(Task).where(
                Task.workspace_id == body.workspace_id,
                Task.idempotency_key == body.idempotency_key,
            )
        )
        if existing is None:
            # Unexpected — re-raise as 500-equivalent.
            raise HTTPException(500, "idempotency conflict but no existing task found") from None
        return TaskOut.from_model(existing)
    await session.refresh(task)

    # Spawn the loop. It manages its own session so the request session can close.
    sm = request.app.state.db_sessionmaker
    settings = request.app.state.settings
    bus = getattr(request.app.state, "event_bus", None)
    hub = getattr(request.app.state, "runner_hub", None)
    dispatcher = getattr(request.app.state, "dispatcher", None)
    parser = getattr(request.app.state, "intent_parser", None)
    if parser is None:
        # Without a configured parser we can't make progress. Surface it in
        # "Needs you" so the user can edit & resubmit (or cancel) rather than
        # silently stalling.
        log.error("intent_parser not configured; task %s -> parse_failed", task.id)
        async with sm() as ds:
            t2 = await ds.get(Task, task.id)
            assert t2 is not None
            svc = TaskStateService(ds, publisher=bus)
            await svc.transition(
                t2.id, TaskStatus.PARSE_FAILED,
                actor="system",
                reason="intent parser not configured",
            )
            await ds.commit()
        return TaskOut.from_model(task)

    parse_loop = IntentParseLoop(
        llm_client=parser._client,  # type: ignore[attr-defined]  — internal field by design
        model=parser._model,  # type: ignore[attr-defined]
        timeout_attempt_1=settings.intent_parser_timeout_seconds,
        timeout_attempt_2=settings.intent_parser_timeout_seconds + 5.0,
    )

    if bus is not None:
        await bus.publish({
            "type": "task.parse_started",
            "task_id": str(task.id),
            "raw_input": raw,
        })

    _spawn_bg(run_parse_loop(
        task_id=task.id,
        raw_input=raw,
        sessionmaker=sm,
        loop=parse_loop,
        hub=hub,
        bus=bus,
        dispatcher=dispatcher,
        hint=body.origin if body.origin in ("voice", "im", "text") else "text",
    ))

    return TaskOut.from_model(task)


async def _get_parent_tasks(session: AsyncSession, child_id: UUID) -> list[Task]:
    stmt = (
        select(Task)
        .join(TaskDependency, TaskDependency.parent_task_id == Task.id)
        .where(TaskDependency.child_task_id == child_id)
    )
    return list((await session.scalars(stmt)).all())


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TaskOut)
async def create_task(
    body: TaskCreateBody,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> TaskOut:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.auth_mode != "disabled":
        require_user(principal)

    # Membership check: a user must be a member of the workspace they're
    # posting into. Without this, a stale activeWorkspaceId in the client
    # would let create_task return 201 for a workspace whose tasks the same
    # user can't read — silent-success, no card on the kanban.
    if not isinstance(principal, ServicePrincipal):
        member = await session.get(
            WorkspaceMember, (body.workspace_id, principal.id)
        )
        if member is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "not a member of this workspace"
            )

    # Idempotency check: if a task with the same (workspace_id, idempotency_key)
    # exists, return it. The unique partial index also catches concurrent races
    # at INSERT time below.
    if body.idempotency_key is not None:
        existing = await session.scalar(
            select(Task).where(
                Task.workspace_id == body.workspace_id,
                Task.idempotency_key == body.idempotency_key,
            )
        )
        if existing is not None:
            counts = await _dep_counts(session, [existing.id])
            return TaskOut.from_model(existing, dependencies_count=counts.get(existing.id, 0))

    # Mode validation: exactly one of (prompt, raw_input).
    has_prompt = body.prompt is not None and body.prompt.strip()
    has_raw = body.raw_input is not None and body.raw_input.strip()
    if has_prompt and has_raw:
        raise HTTPException(400, "prompt and raw_input are mutually exclusive")
    if not has_prompt and not has_raw:
        raise HTTPException(400, "must provide either prompt or raw_input")

    if has_raw:
        # Async raw-input mode. Create parsing-state task; loop runs in background.
        return await _create_raw_input_task(body=body, session=session, request=request)

    # Structured-form path. Re-validate required fields the schema couldn't enforce
    # because they're optional now.
    if not body.agent:
        raise HTTPException(400, "agent is required when posting structured tasks")
    if not body.title:
        raise HTTPException(400, "title is required when posting structured tasks")

    if settings is not None and getattr(settings, "reject_offline_agents", False):
        hub = getattr(request.app.state, "runner_hub", None)
        if hub is not None:
            known = {c["capability"] for c in hub.available_capabilities()}
            if body.agent not in known:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"agent '{body.agent}' has no connected runner",
                )

    # Structured form already carries title/agent/prompt — there's nothing
    # to parse and nothing to review, so it goes straight to the queue
    # (BLOCKED if it has unfinished dependency parents, else PENDING).
    # DRAFT no longer exists; "submit is submit" (docs/parsing-ux-rework.md).
    has_deps = bool(body.depends_on)
    task = Task(
        workspace_id=body.workspace_id,
        title=body.title,
        description=body.description,
        prompt=body.prompt or "",
        origin=body.origin,
        agent=body.agent,
        repo=body.repo,
        base_branch=body.base_branch,
        isolation=body.isolation,
        timeout_seconds=body.timeout_seconds,
        status=TaskStatus.BLOCKED.value if has_deps else TaskStatus.PENDING.value,
        idempotency_key=body.idempotency_key,
    )
    session.add(task)
    await session.flush()  # get task.id before inserting deps / linking attachments
    await _validate_and_record_deps(session, task=task, depends_on=body.depends_on)
    await _link_attachments(session, task=task, attachment_ids=body.attachment_ids)
    await session.commit()
    await session.refresh(task)

    # Kick the dispatcher for immediately-runnable (no-dep) tasks so they
    # don't wait for the next dispatch-interval pass.
    if not has_deps:
        dispatcher = getattr(request.app.state, "dispatcher", None)
        if dispatcher is not None:
            sm = request.app.state.db_sessionmaker

            async def _dispatch_bg() -> None:
                async with sm() as ds:
                    try:
                        await dispatcher.try_dispatch_pending(ds)
                    except Exception as e:  # noqa: BLE001
                        log.warning("background dispatch after create failed: %s", e)

            _spawn_bg(_dispatch_bg())

    return TaskOut.from_model(task, dependencies_count=len(body.depends_on))


@router.get("", response_model=TaskListOut)
async def list_tasks(
    session: SessionDep,
    principal: PrincipalDep,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
) -> TaskListOut:
    visible = await get_visible_workspace_ids(session, principal)
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    if visible is not None:
        stmt = stmt.where(Task.workspace_id.in_(visible))
    rows = (await session.scalars(stmt)).all()
    ids = [r.id for r in rows]
    counts = await _dep_counts(session, ids)
    att_counts = await _attachment_counts(session, ids)
    return TaskListOut(items=[
        TaskOut.from_model(
            r,
            dependencies_count=counts.get(r.id, 0),
            attachments_count=att_counts.get(r.id, 0),
        )
        for r in rows
    ])


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> TaskOut:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and task.workspace_id not in visible:
        raise HTTPException(404, "task not found")  # 404 not 403 to avoid enumeration
    counts = await _dep_counts(session, [task_id])
    return TaskOut.from_model(task, dependencies_count=counts.get(task_id, 0))


@router.get("/{task_id}/logs", response_model=TaskLogsOut)
async def list_task_logs(
    task_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    stream: str = Query("all", pattern=r"^(stdout|stderr|all)$"),
    limit: int = Query(2000, ge=1, le=5000),
) -> TaskLogsOut:
    # Reuse the get_task visibility shape: 404 for missing OR foreign workspace.
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and task.workspace_id not in visible:
        raise HTTPException(404, "task not found")

    # Total count for this task (and stream filter, if any).
    total_stmt = select(func.count()).select_from(TaskLog).where(TaskLog.task_id == task_id)
    if stream != "all":
        total_stmt = total_stmt.where(TaskLog.stream == stream)
    total = (await session.execute(total_stmt)).scalar_one()

    # Pull the most recent N rows by seq DESC, then reverse to chronological.
    rows_stmt = (
        select(TaskLog)
        .where(TaskLog.task_id == task_id)
        .order_by(TaskLog.seq.desc())
        .limit(limit)
    )
    if stream != "all":
        rows_stmt = rows_stmt.where(TaskLog.stream == stream)
    rows = list((await session.scalars(rows_stmt)).all())
    rows.reverse()  # chronological for display

    items = [
        TaskLogEntry(seq=r.seq, stream=r.stream, data=r.data, created_at=r.created_at)
        for r in rows
    ]
    return TaskLogsOut(
        items=items,
        total=total,
        returned=len(items),
        truncated=total > len(items),
    )


@router.get("/{task_id}/turns", response_model=TaskTurnsOut)
async def list_task_turns(
    task_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> TaskTurnsOut:
    # Same visibility shape as get_task: 404 for missing OR foreign workspace.
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and task.workspace_id not in visible:
        raise HTTPException(404, "task not found")

    rows = (
        await session.scalars(
            select(TaskTurn)
            .where(TaskTurn.task_id == task_id)
            .order_by(TaskTurn.seq.asc())
        )
    ).all()
    items = [
        TaskTurnOut(seq=r.seq, role=r.role, content=r.content, created_at=r.created_at)
        for r in rows
    ]
    return TaskTurnsOut(items=items)


async def _transition(
    task_id: UUID,
    target: TaskStatus,
    session: AsyncSession,
    actor: str,
    bus=None,
    reason: str | None = None,
) -> TaskOut:
    svc = TaskStateService(session, publisher=bus)
    try:
        await svc.transition(task_id, target, actor=actor, reason=reason)
    except IllegalTransition as e:
        raise HTTPException(409, str(e)) from e
    except LookupError:
        raise HTTPException(404, "task not found") from None
    await session.commit()
    task = await session.get(Task, task_id)
    assert task is not None
    counts = await _dep_counts(session, [task_id])
    return TaskOut.from_model(task, dependencies_count=counts.get(task_id, 0))


class TaskPatchBody(BaseModel):
    """Quick-review edit for parser-produced drafts. Only fields a user is
    likely to fix during review are mutable. Status is not — use
    /submit or /transition for that."""
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=280)
    prompt: str | None = None
    agent: str | None = None
    repo: str | None = None


@router.patch("/{task_id}", response_model=TaskOut)
async def patch_task(
    task_id: UUID,
    body: TaskPatchBody,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> TaskOut:
    """Update mutable fields on a task that hasn't started executing.
    Used by the parse_failed "edit & resubmit" flow."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.auth_mode != "disabled":
        require_user(principal)

    visible = await get_visible_workspace_ids(session, principal)
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    if visible is not None and task.workspace_id not in visible:
        raise HTTPException(404, "task not found")

    # Only allow edits while the task hasn't started executing.
    if task.status not in (TaskStatus.PARSING.value, TaskStatus.PARSE_FAILED.value):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot edit task in status '{task.status}'",
        )

    if body.description is not None:
        task.description = body.description

    if body.title is not None:
        task.title = body.title
    if body.prompt is not None:
        task.prompt = body.prompt
    if body.agent is not None:
        task.agent = body.agent
    if body.repo is not None:
        task.repo = body.repo
    await session.commit()
    await session.refresh(task)
    counts = await _dep_counts(session, [task.id])
    return TaskOut.from_model(task, dependencies_count=counts.get(task.id, 0))


@router.post("/{task_id}/submit", response_model=TaskOut)
async def submit_task(
    task_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> TaskOut:
    visible = await get_visible_workspace_ids(session, principal)
    # Check access before mutating.
    task_check = await session.get(Task, task_id)
    if task_check is None:
        raise HTTPException(404, "task not found")
    if visible is not None and task_check.workspace_id not in visible:
        raise HTTPException(404, "task not found")

    bus = getattr(request.app.state, "event_bus", None)
    # Inspect deps before transitioning.
    parents = await _get_parent_tasks(session, task_id)
    if parents:
        terminal_bad = [p for p in parents if p.status in {"failed", "cancelled"}]
        if terminal_bad:
            result = await _transition(
                task_id, TaskStatus.CANCELLED, session, actor="system", bus=bus,
                reason="dependency_failed",
            )
            return result
        all_done = all(p.status == "done" for p in parents)
        target = TaskStatus.PENDING if all_done else TaskStatus.BLOCKED
    else:
        target = TaskStatus.PENDING

    result = await _transition(task_id, target, session, actor="user", bus=bus)

    if target is TaskStatus.PENDING:
        dispatcher = getattr(request.app.state, "dispatcher", None)
        if dispatcher is not None:
            # Fire-and-forget the dispatch step. try_dispatch_pending does
            # bedrock-embedding-backed memory retrieval per task, which can
            # take seconds — awaiting it inline holds the HTTP response
            # for the whole duration and the UI's "Submitting…" spinner
            # never clears. The transition above is already committed, so
            # the response can return now; the dispatcher loop will pick
            # up the PENDING task on its next pass anyway, but kicking it
            # explicitly avoids the dispatch-interval lag.
            sm = request.app.state.db_sessionmaker

            async def _dispatch_bg() -> None:
                async with sm() as dispatch_sess:
                    try:
                        await dispatcher.try_dispatch_pending(dispatch_sess)
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "background dispatch after submit failed: %s", e,
                        )

            _spawn_bg(_dispatch_bg())
    return result


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(
    task_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> TaskOut:
    visible = await get_visible_workspace_ids(session, principal)
    task_check = await session.get(Task, task_id)
    if task_check is None:
        raise HTTPException(404, "task not found")
    if visible is not None and task_check.workspace_id not in visible:
        raise HTTPException(404, "task not found")

    bus = getattr(request.app.state, "event_bus", None)
    return await _transition(task_id, TaskStatus.CANCELLED, session, actor="user", bus=bus)


@router.post("/{task_id}/approve", response_model=TaskOut)
async def approve_task(
    task_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> TaskOut:
    """User-side accept of an in_review task. Transitions to DONE."""
    visible = await get_visible_workspace_ids(session, principal)
    task_check = await session.get(Task, task_id)
    if task_check is None:
        raise HTTPException(404, "task not found")
    if visible is not None and task_check.workspace_id not in visible:
        raise HTTPException(404, "task not found")

    # Approve is only meaningful from IN_REVIEW, and the state machine alone
    # cannot enforce that: RUNNING -> DONE is legal there because that is how
    # the runner reports success. Without this guard, approving a task that is
    # still running would mark it done while the agent keeps working.
    current = TaskStatus(task_check.status)
    if current is not TaskStatus.IN_REVIEW:
        raise HTTPException(409, f"cannot approve task in status '{current.value}'")

    bus = getattr(request.app.state, "event_bus", None)
    return await _transition(
        task_id, TaskStatus.DONE, session, actor="user", bus=bus, reason="approved"
    )


@router.post("/{task_id}/rerun", response_model=TaskOut)
async def rerun_task(
    task_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> TaskOut:
    """Re-run the same task with a clean slate. Allowed from in_review,
    done, failed, or cancelled. Deletes prior task_logs and task_turns
    so the drawer shows only this run's output, then transitions to
    PENDING and kicks the dispatcher. task_events (state transition
    audit) is preserved.

    For "continue this conversation" semantics, callers should use
    /respond on awaiting_input tasks instead — that path keeps turns."""
    from sqlalchemy import delete as sa_delete

    visible = await get_visible_workspace_ids(session, principal)
    task_check = await session.get(Task, task_id)
    if task_check is None:
        raise HTTPException(404, "task not found")
    if visible is not None and task_check.workspace_id not in visible:
        raise HTTPException(404, "task not found")

    # Validate the transition first so we don't wipe history on a request
    # the state machine would have rejected anyway. _transition will run
    # the same check, but doing it here keeps DELETE side-effect-free
    # for illegal calls.
    from taskdeck_core.state.machine import can_transition
    current = TaskStatus(task_check.status)
    if not can_transition(current, TaskStatus.PENDING):
        raise HTTPException(
            409, f"cannot rerun task in status '{current.value}'"
        )

    # Clean slate: drop prior run's logs and conversation turns. Same
    # session as the transition, so a downstream failure rolls back
    # both atomically.
    await session.execute(sa_delete(TaskLog).where(TaskLog.task_id == task_id))
    await session.execute(sa_delete(TaskTurn).where(TaskTurn.task_id == task_id))

    # Reset per-run fields so the next run starts truly fresh. summary
    # and exit_code from the prior attempt would otherwise stick around
    # in the UI until the new run finishes.
    task_check.exit_code = None
    task_check.summary = None
    task_check.started_at = None
    task_check.assigned_runner_id = None

    bus = getattr(request.app.state, "event_bus", None)
    result = await _transition(
        task_id, TaskStatus.PENDING, session, actor="user", bus=bus, reason="rerun"
    )

    dispatcher = getattr(request.app.state, "dispatcher", None)
    if dispatcher is not None:
        # Same fire-and-forget pattern as submit_task — see comment there.
        sm = request.app.state.db_sessionmaker

        async def _dispatch_bg() -> None:
            async with sm() as dispatch_sess:
                try:
                    await dispatcher.try_dispatch_pending(dispatch_sess)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "background dispatch after rerun failed: %s", e,
                    )

        _spawn_bg(_dispatch_bg())
    return result


class TaskRespondBody(BaseModel):
    content: str


@router.post("/{task_id}/respond", response_model=TaskOut)
async def respond_to_task(
    task_id: UUID,
    body: TaskRespondBody,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> TaskOut:
    """User reply to an agent's <ccpt:ask>. Writes a user turn and
    returns the task to pending so the dispatcher picks it back up."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.auth_mode != "disabled":
        require_user(principal)

    if len(body.content) > 8192:
        raise HTTPException(400, "content exceeds 8192 characters")
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "content cannot be empty")

    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")

    # Workspace-scoped write: 403 for non-member.
    if not isinstance(principal, ServicePrincipal):
        member = await session.get(WorkspaceMember, (task.workspace_id, principal.id))
        if member is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "not a member of this workspace"
            )

    if task.status != TaskStatus.AWAITING_INPUT.value:
        raise HTTPException(
            400,
            f"task is in '{task.status}' state, cannot respond",
        )

    # Append user turn at next seq.
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(TaskTurn.seq), -1) + 1).where(TaskTurn.task_id == task_id)
        )
    ).scalar_one()
    session.add(TaskTurn(
        task_id=task_id,
        seq=next_seq,
        role="user",
        content=content,
        created_at=datetime.now(UTC),
    ))

    bus = getattr(request.app.state, "event_bus", None)
    svc = TaskStateService(session, publisher=bus)
    try:
        await svc.transition(
            task_id, TaskStatus.PENDING,
            actor="user",
            reason="user_responded",
        )
    except IllegalTransition as e:
        raise HTTPException(500, f"unexpected state transition: {e}") from e

    await session.commit()
    await session.refresh(task)

    # Kick the dispatcher so the resumed task is picked up promptly.
    # Fire-and-forget — same reason as submit_task.
    dispatcher = getattr(request.app.state, "dispatcher", None)
    if dispatcher is not None:
        sm = request.app.state.db_sessionmaker

        async def _dispatch_bg() -> None:
            async with sm() as dispatch_sess:
                try:
                    await dispatcher.try_dispatch_pending(dispatch_sess)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "background dispatch after respond failed: %s", e,
                    )

        _spawn_bg(_dispatch_bg())

    counts = await _dep_counts(session, [task_id])
    return TaskOut.from_model(task, dependencies_count=counts.get(task_id, 0))


class TransitionBody(BaseModel):
    to: str = Field(pattern=r"^(draft|pending|blocked|running|done|failed|cancelled)$")
    reason: str | None = None


async def _caller_is_admin(
    session: AsyncSession,
    principal: User | ServicePrincipal,
    workspace_id,
    settings,
) -> bool:
    if settings is None or settings.auth_mode == "disabled":
        return True
    if isinstance(principal, ServicePrincipal):
        return False  # runners use CRP, not REST admin path
    stmt = select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == principal.id,
        WorkspaceMember.role == "owner",
    )
    return (await session.scalars(stmt)).first() is not None


@router.post("/{task_id}/transition", response_model=TaskOut)
async def transition_task(
    task_id: UUID,
    body: TransitionBody,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> TaskOut:
    settings = getattr(request.app.state, "settings", None)

    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and task.workspace_id not in visible:
        raise HTTPException(404, "task not found")

    is_admin = await _caller_is_admin(session, principal, task.workspace_id, settings)

    if not is_admin and isinstance(principal, ServicePrincipal):
        raise HTTPException(403, "service principals cannot use this endpoint")

    target = TaskStatus(body.to)
    bus = getattr(request.app.state, "event_bus", None)
    svc = TaskStateService(session, publisher=bus)

    if is_admin:
        actor_id = str(principal.id) if isinstance(principal, User) else None
        await svc.admin_transition(
            task_id, target,
            actor="admin", actor_id=actor_id, reason=body.reason,
        )
    else:
        try:
            actor_id = str(principal.id) if isinstance(principal, User) else None
            await svc.transition(task_id, target, actor="user", actor_id=actor_id, reason=body.reason)
        except IllegalTransition as e:
            raise HTTPException(409, str(e)) from e

    await session.commit()

    if target is TaskStatus.PENDING:
        dispatcher = getattr(request.app.state, "dispatcher", None)
        if dispatcher is not None:
            # Fire-and-forget — see submit_task for the rationale.
            sm = request.app.state.db_sessionmaker

            async def _dispatch_bg() -> None:
                async with sm() as ds:
                    try:
                        await dispatcher.try_dispatch_pending(ds)
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "background dispatch after admin transition failed: %s", e,
                        )

            _spawn_bg(_dispatch_bg())

    refreshed = await session.get(Task, task_id)
    assert refreshed is not None
    return TaskOut.from_model(refreshed, dependencies_count=0)


class TaskArchiveBody(BaseModel):
    archive_key: str
    archived_at: datetime
    size_bytes: int | None = None


@router.post("/{task_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def record_task_archive(
    task_id: UUID,
    body: TaskArchiveBody,
    session: SessionDep,
    principal: PrincipalDep,
) -> None:
    """Service callback from sandbox-host's workspace_gc — records that
    the task's worktree was tar.gz'd to S3 before deletion. Only the
    runner bearer token (ServicePrincipal) is allowed — kanban users
    don't initiate archives. Idempotent on retry: re-POSTing the same
    key just updates archived_at."""
    if not isinstance(principal, ServicePrincipal):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "archive callback is service-only"
        )
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    task.archive_key = body.archive_key
    task.archived_at = body.archived_at
    await session.commit()


@router.get("/{task_id}/dependencies", response_model=DependenciesOut)
async def get_task_deps(
    task_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> DependenciesOut:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and task.workspace_id not in visible:
        raise HTTPException(404, "task not found")
    parents = await _get_parent_tasks(session, task_id)
    return DependenciesOut(
        parents=[DepParentOut(id=p.id, title=p.title, status=p.status) for p in parents]
    )


class TaskAttachmentOut(BaseModel):
    id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    created_at: datetime


class TaskAttachmentsOut(BaseModel):
    items: list[TaskAttachmentOut]


@router.get("/{task_id}/attachments", response_model=TaskAttachmentsOut)
async def get_task_attachments(
    task_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> TaskAttachmentsOut:
    """Files the user uploaded for this task. Drawer + card both use
    this. ACL piggybacks on workspace visibility so the same anti-
    enumeration 404 applies."""
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and task.workspace_id not in visible:
        raise HTTPException(404, "task not found")
    rows = (
        await session.scalars(
            select(TaskAttachment)
            .where(TaskAttachment.task_id == task_id)
            .order_by(TaskAttachment.created_at.asc())
        )
    ).all()
    return TaskAttachmentsOut(
        items=[
            TaskAttachmentOut(
                id=r.id,
                original_filename=r.original_filename,
                content_type=r.content_type,
                size_bytes=r.size_bytes,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )
