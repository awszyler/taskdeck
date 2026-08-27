from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from taskdeck_core.db.models import ImIdentityLink, IntentParseLog, Task
from taskdeck_core.intent.schema import IntentContext, IntentInput
from taskdeck_core.state.machine import IllegalTransition, TaskStatus
from taskdeck_core.state.service import TaskStateService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from taskdeck_core.intent.parser import IntentParser

    from .binder import BindCodeCache

log = logging.getLogger(__name__)


async def resolve_link(session: AsyncSession, *, external_id: str) -> ImIdentityLink | None:
    stmt = select(ImIdentityLink).where(
        ImIdentityLink.platform == "wecom",
        ImIdentityLink.external_id == external_id,
    )
    return (await session.scalars(stmt)).first()


async def handle_bind(
    *,
    code: str,
    external_id: str,
    session: AsyncSession,
    cache: BindCodeCache,
) -> str:
    """Return the reply text to send back to WeCom."""
    entry = cache.consume(code.strip())
    if entry is None:
        return "❌ Bind failed: code is invalid or expired."

    # Idempotent: if this external_id is already linked, replace with the new workspace.
    existing = await resolve_link(session, external_id=external_id)
    if existing:
        existing.workspace_id = entry.workspace_id
        if entry.user_id:
            existing.user_id = entry.user_id
        await session.commit()
        return f"✓ Re-bound to workspace {entry.workspace_id}."

    link = ImIdentityLink(
        workspace_id=entry.workspace_id,
        user_id=entry.user_id,
        platform="wecom",
        external_id=external_id,
        created_at=datetime.now(UTC),
    )
    session.add(link)
    await session.commit()
    return "✓ Bound! You can now create tasks by sending messages here."


async def handle_status(
    *,
    external_id: str,
    session: AsyncSession,
    limit: int = 5,
) -> str:
    link = await resolve_link(session, external_id=external_id)
    if link is None:
        return "❌ You're not bound. Open the web UI and click 'Bind WeCom'."

    stmt = (
        select(Task)
        .where(Task.workspace_id == link.workspace_id)
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    rows = (await session.scalars(stmt)).all()
    if not rows:
        return "No tasks yet."
    lines = []
    for i, t in enumerate(rows, 1):
        short = str(t.id)[:8]
        lines.append(f"{i}. {t.title[:40]}  [{short} · {t.status}]")
    return "\n".join(lines)


async def handle_free_text(
    *,
    content: str,
    external_id: str,
    session: AsyncSession,
    parser: IntentParser,
    public_base_url: str,
    sessionmaker: async_sessionmaker,
    dispatcher,
    available_capabilities: list[dict[str, str]] | None = None,
) -> str:
    """Route a free-text WeCom message through the Intent Parser and create a task."""
    link = await resolve_link(session, external_id=external_id)
    if link is None:
        return "❌ You're not bound. DM `/bind <code>` first (get the code in the web UI)."

    # Collect recent repos for intent-parser context (last 5 distinct repos used in this workspace).
    recent_repos_stmt = (
        select(Task.repo)
        .where(Task.workspace_id == link.workspace_id, Task.repo.is_not(None))
        .order_by(Task.created_at.desc())
        .limit(20)
    )
    recent = [r for r in (await session.scalars(recent_repos_stmt)).all() if r]
    seen: list[str] = []
    for r in recent:
        if r not in seen:
            seen.append(r)
        if len(seen) >= 5:
            break

    t0 = time.monotonic()
    parsed = await parser.parse(
        IntentInput(
            raw_input=content,
            hint="im",
            context=IntentContext(recent_repos=seen),
        ),
        available_capabilities=available_capabilities,
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    task = Task(
        workspace_id=link.workspace_id,
        title=parsed.title,
        prompt=parsed.prompt,
        origin="im",
        raw_input=content,
        intent_confidence=parsed.confidence,
        agent=parsed.agent or "shell",
        repo=parsed.repo,
        base_branch=parsed.base_branch or "main",
        isolation="worktree",
        status=TaskStatus.PENDING.value,
        created_by=link.user_id,
    )
    session.add(task)
    await session.flush()  # populate task.id before writing the log row

    session.add(
        IntentParseLog(
            task_id=task.id,
            raw_input=content,
            parsed_output=parsed.model_dump(mode="json"),
            model=getattr(parser, "_model", "unknown"),
            latency_ms=latency_ms,
            success=parsed.confidence > 0,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await session.refresh(task)

    async with sessionmaker() as dispatch_sess:
        await dispatcher.try_dispatch_pending(dispatch_sess)

    short = str(task.id)[:8]
    url = f"{public_base_url.rstrip('/')}/?task={task.id}"
    note = f"(low confidence {parsed.confidence:.2f})" if parsed.confidence < 0.5 else ""
    return f"✓ Task #{short} created{' ' + note if note else ''}\n{parsed.title}\n{url}"


async def handle_cancel(
    *,
    target: str,
    external_id: str,
    session: AsyncSession,
) -> str:
    link = await resolve_link(session, external_id=external_id)
    if link is None:
        return "❌ You're not bound."

    target = target.strip()
    if not target:
        return "Usage: /cancel <task_id_or_prefix>"

    # Accept full UUID or a prefix (must uniquely identify).
    stmt = (
        select(Task)
        .where(Task.workspace_id == link.workspace_id)
        .order_by(Task.created_at.desc())
    )
    rows = (await session.scalars(stmt)).all()
    matches = [t for t in rows if str(t.id).startswith(target)]
    if len(matches) == 0:
        return f"❌ No task starts with {target}."
    if len(matches) > 1:
        return f"❌ Ambiguous — {len(matches)} tasks start with {target}. Use a longer prefix."

    task = matches[0]
    svc = TaskStateService(session)
    try:
        await svc.transition(task.id, TaskStatus.CANCELLED, actor="user", reason="wecom_cancel")
    except IllegalTransition as e:
        return f"❌ Can't cancel: {e}"
    except LookupError:
        return "❌ Task not found."
    await session.commit()
    return f"✓ Cancelled task {str(task.id)[:8]}."
