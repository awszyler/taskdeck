from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from taskdeck_core.db.models import Task, TaskAttachment, TaskTurn, Workspace
from taskdeck_core.deps.injector import collect_dependency_outputs
from taskdeck_core.state.machine import TaskStatus
from taskdeck_core.state.service import TaskStateService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from taskdeck_core.crp.hub import RunnerHub

log = logging.getLogger(__name__)

# Agents that honor the <ccpt:ask> protocol. Shell is excluded — it has no
# judgment for "should I ask".
ASK_PROTOCOL_AGENTS_PREFIXES = ("claude-code", "agentcore-")

ASK_PROTOCOL_HEADER = """\
You are a task runner integrated into a kanban system.

You have full tool access in a sandboxed workspace; do not ask the user
to approve tool use, file writes, or command execution — those are
already authorized by the task assignment.

Use <ccpt:ask> ONLY when you need clarifying information from the user
that is not derivable from the task description (e.g. ambiguous scope,
missing credentials, choice between equally valid approaches). In that
case, output exactly:
<ccpt:ask>your question to the user, in plain text</ccpt:ask>
Then exit. Do NOT take destructive actions while uncertain.

Otherwise, complete the task as instructed.

# Output manifest (P6.4)

When you finish, if you produced anything the user should look at,
write a manifest at `.taskdeck/output.yml` declaring how each
output should be viewed. The system reads this file to populate the
"Open" menu on the kanban card. Format:

  outputs:
    - kind: interactive    # browser-rendered, sandboxed
      entry: counter.html  # path relative to workspace root
      label: 计数器 demo
    - kind: document       # markdown / text / pdf, drawer viewer
      entry: README.md
      label: 实现说明
    - kind: code           # source files, drawer viewer
      entry: src/
      label: 源码
    - kind: data           # csv / json, drawer table
      entry: results.csv
    - kind: image          # png / svg, drawer inline
      entry: diagram.png
    - kind: archive        # zip / binary, download only
      entry: bundle.zip

For interactive outputs that need a server (not just a static
.html), include runtime/install/start/port:

    - kind: interactive
      entry: main.py
      runtime: python              # or "node" or "static"
      install: pip install -r requirements.txt
      start: uvicorn main:app --host 0.0.0.0 --port 8000
      port: 8000

If you don't write a manifest, the system scans the workspace and
guesses — that works for trivial cases (single .html file, plain
README.md) but is unreliable for anything else. Writing a manifest
takes 5 seconds and gives the user a much better experience.

---

Task:
"""

PRIOR_TURNS_BYTE_CAP = 32 * 1024  # 32 KB total content


def _agent_supports_ask(agent: str) -> bool:
    return any(agent.startswith(p) for p in ASK_PROTOCOL_AGENTS_PREFIXES)


def _build_prompt_with_protocol(original: str, agent: str) -> str:
    if not _agent_supports_ask(agent):
        return original
    return ASK_PROTOCOL_HEADER + original


async def _gather_prior_turns(session, task_id) -> list[dict]:
    """Returns prior turns in chronological order, capped at PRIOR_TURNS_BYTE_CAP
    bytes of total content. If oldest turns are dropped, prepends a placeholder."""
    rows = (
        await session.scalars(
            select(TaskTurn)
            .where(TaskTurn.task_id == task_id)
            .order_by(TaskTurn.seq.asc())
        )
    ).all()
    if not rows:
        return []

    # Walk newest-to-oldest, accumulating bytes.
    included: list = []
    total_bytes = 0
    for row in reversed(rows):
        size = len(row.content.encode("utf-8"))
        if total_bytes + size > PRIOR_TURNS_BYTE_CAP and included:
            break
        included.append(row)
        total_bytes += size
    included.reverse()

    n_dropped = len(rows) - len(included)
    out = []
    if n_dropped > 0:
        out.append({
            "role": "agent",
            "content": f"({n_dropped} earlier turns omitted for length)",
        })
    out.extend({"role": r.role, "content": r.content} for r in included)
    return out


class Dispatcher:
    """Assigns pending tasks to idle runners."""

    def __init__(self, hub: RunnerHub, publisher=None, artifact_store=None):
        self._hub = hub
        self._publisher = publisher
        self._artifact_store = artifact_store
        self._embedding_client = None
        self._settings = None

    async def try_dispatch_pending(self, session: AsyncSession) -> int:
        """Find pending tasks and assign to idle runners. Returns count dispatched."""
        stmt = (
            select(Task)
            .where(Task.status == TaskStatus.PENDING.value)
            .order_by(Task.created_at.asc())
        )
        pending = (await session.scalars(stmt)).all()

        dispatched = 0
        svc = TaskStateService(session, publisher=self._publisher)
        for task in pending:
            conn = self._hub.pick_for(task.agent)
            if conn is None:
                continue
            # Reserve slot synchronously
            conn.increment_inflight()
            task.assigned_runner_id = None  # not a UUID-keyed registry in M1
            await svc.transition(
                task.id, TaskStatus.RUNNING, actor="system", reason="dispatch"
            )
            dep_outputs: list[dict] = []
            if task.agent != "shell" and self._artifact_store is not None:
                dep_outputs = await collect_dependency_outputs(
                    session,
                    child_task_id=task.id,
                    artifact_store=self._artifact_store,
                )

            memory: list[dict] = []
            if (
                self._settings is not None
                and self._settings.memory_enabled
                and self._embedding_client is not None
            ):
                try:
                    from taskdeck_core.memory.retriever import retrieve

                    memory = await retrieve(
                        session,
                        workspace_id=task.workspace_id,
                        query_text=f"{task.title}\n{task.prompt}",
                        embedding_client=self._embedding_client,
                        top_k=self._settings.memory_top_k,
                        per_cap=self._settings.memory_per_chunk_cap_bytes,
                        total_cap=self._settings.memory_total_cap_bytes,
                    )
                except Exception as e:
                    log.warning("memory retrieval failed for task %s: %s", task.id, e)
                    memory = []

            ws = await session.get(Workspace, task.workspace_id)
            if ws is None:
                # Defensive — task references a missing workspace. Skip.
                log.warning(
                    "task %s references missing workspace %s, skipping",
                    task.id, task.workspace_id,
                )
                continue

            wrapped_prompt = _build_prompt_with_protocol(task.prompt, task.agent)
            prior_turns = await _gather_prior_turns(session, task.id)

            # P7 multimodal inputs: attach the user-uploaded files. The
            # runner pulls them from core's /attachments/<id>/file
            # endpoint at task start and writes them into cwd.
            attachment_rows = (await session.scalars(
                select(TaskAttachment).where(TaskAttachment.task_id == task.id)
            )).all()
            attachments_payload = [
                {
                    "id": str(a.id),
                    "filename": a.original_filename,
                    "content_type": a.content_type,
                    "size_bytes": a.size_bytes,
                }
                for a in attachment_rows
            ]

            await conn.socket.send_json(
                {
                    "type": "task.assign",
                    "task_id": str(task.id),
                    "payload": {
                        "agent": task.agent,
                        "workspace_slug": ws.slug,
                        "repo": task.repo,
                        "base_branch": task.base_branch,
                        "prompt": wrapped_prompt,
                        "isolation": task.isolation,
                        "timeout_seconds": task.timeout_seconds,
                        "dependency_outputs": dep_outputs,
                        "memory": memory,
                        "prior_turns": prior_turns,
                        "attachments": attachments_payload,
                    },
                }
            )
            dispatched += 1
        await session.commit()
        return dispatched
