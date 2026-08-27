from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import httpx
import websockets
from pydantic import ValidationError
from taskdeck_proto.crp import (
    Hello,
    TaskAssign,
    TaskAwaitingInput,
    TaskFailed,
    TaskFinished,
    TaskLog,
    TaskStarted,
    parse_message,
)

from .agents.claude_code import ClaudeCodeExecutor
from .agents.kiro_cli import KiroCliExecutor
from .agents.shell import ShellExecutor
from .attachments import (
    AttachmentError,
    download_attachments,
    render_attachments_block,
)
from .capability_descriptions import build_descriptions
from .deps import render_dependency_outputs
from .memory import render_memory
from .workspace import ArtifactPayload, Workspace

if TYPE_CHECKING:
    from .agents.base import AgentRuntime
    from .settings import RunnerSettings

log = logging.getLogger(__name__)


def _build_capabilities_block() -> str:
    """Tell AI agents what runner-level affordances are wired up so
    they pick the right tool path on the first try.

    Currently only GitHub: when GH_TOKEN is exported (set by main.py
    when TD_GITHUB_TOKEN is in .env.runner), the agent has full
    GitHub write access via `gh` and via git+HTTPS. Without this
    hint, claude-code's prior runs spent half a turn probing
    `~/.ssh`, `gh auth status`, and `env | grep TOKEN`, then fell
    back to the read-only deploy key — see tasks 8e992a0c, 649065cb,
    and 2d0d0d38 from 2026-06-03.

    Returns "" when no capabilities are wired (default — block is
    empty, prompt is unchanged).
    """
    import os
    sections: list[str] = []
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        sections.append(
            "GitHub: GH_TOKEN and GITHUB_TOKEN are set in your environment. "
            "Use `gh` (gh repo create / gh repo clone / gh pr / gh api) and "
            "`git` over HTTPS for any GitHub operation — DO NOT fall back to "
            "the read-only SSH deploy key in ~/.ssh/taskdeck_deploy. "
            "`gh auth setup-git` has already been run, so `git push` over "
            "https://github.com/... uses the token transparently."
        )
    if not sections:
        return ""
    body = "\n".join(f"- {s}" for s in sections)
    return f"<capabilities>\n{body}\n</capabilities>\n\n"


# Hard wall-clock cap on a single agent run, regardless of the per-task
# timeout_seconds. Belt-and-suspenders for the deadlock class: even if
# every other safety net fails (executor pump dies silently, subprocess
# doesn't exit on its own, sentinel never reaches the consumer), no
# task can hold a runner slot for more than this. 12h covers our
# longest legitimate single-agent runs (large data analysis, long PPT
# builds) with headroom; anything above this is a bug.
_AGENT_RUN_HARD_TIMEOUT_SECONDS = 12 * 60 * 60


class CRPClient:
    def __init__(self, settings: RunnerSettings, executor: AgentRuntime | None = None):
        self._s = settings
        self._exec_override = executor
        self._seq_by_task: dict[str, int] = {}
        self._stopping = False
        self._inflight_tasks: set[asyncio.Task] = set()

    def request_stop(self) -> None:
        """Signal the client to stop accepting new tasks and exit the connect loop."""
        self._stopping = True

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                async with websockets.connect(
                    self._s.core_ws_url,
                    additional_headers={"Authorization": f"Bearer {self._s.token}"},
                ) as ws:
                    await self._session(ws, stop_event)
                    backoff = 1.0
            except Exception as e:  # noqa: BLE001
                if self._stopping:
                    break
                log.warning("connection error: %s; reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        # Drain any in-flight tasks before returning.
        if self._inflight_tasks:
            log.info("waiting for %d in-flight task(s) to finish", len(self._inflight_tasks))
            await asyncio.gather(*list(self._inflight_tasks), return_exceptions=True)

    def _build_capabilities(self) -> list[str]:
        caps = ["shell"]
        if self._s.claude_code_bin:
            caps.append("claude-code")
        if self._s.kiro_cli_bin:
            caps.append("kiro-cli")
        if self._s.openclaw_bin:
            caps.append("openclaw")
        if self._s.hermes_bin:
            caps.append("hermes")
        if self._s.codex_bin:
            caps.append("codex")
        if self._s.agentcore_enabled:
            for agent_id in self._s.agentcore_agent_ids:
                caps.append(f"agentcore-{agent_id}")
        return caps

    async def _session(self, ws: Any, stop_event: asyncio.Event | None = None) -> None:
        caps = self._build_capabilities()
        hello = Hello(
            runner_id=self._s.runner_name,
            capabilities=caps,
            capability_descriptions=build_descriptions(self._s),
            max_parallel=self._s.max_parallel,
            isolation_modes=["worktree"],
            version="0.0.1",
        )
        await ws.send(hello.model_dump_json())
        welcome_raw = await ws.recv()
        log.info("welcome: %s", welcome_raw)

        async for raw in ws:
            if self._stopping:
                log.info("stopping; not dispatching new tasks from WS")
                break
            try:
                msg = parse_message(_loads(raw))
            except (ValidationError, ValueError):
                log.warning("bad message: %r", raw)
                continue
            if isinstance(msg, TaskAssign):
                t = asyncio.create_task(self._run_task(ws, msg))
                self._inflight_tasks.add(t)
                t.add_done_callback(self._inflight_tasks.discard)

    async def _run_task(self, ws_socket: Any, assign: TaskAssign) -> None:
        _t0 = time.monotonic()
        _exit_status = "error"
        try:
            await self._run_task_inner(ws_socket, assign)
            _exit_status = "done"
        except Exception:
            _exit_status = "error"
            raise
        finally:
            _elapsed = time.monotonic() - _t0
            _agent = assign.payload.agent
            try:
                from .metrics import TASK_DURATION_SECONDS, TASKS_TOTAL
                TASK_DURATION_SECONDS.labels(agent=_agent).observe(_elapsed)
                TASKS_TOTAL.labels(agent=_agent, exit_status=_exit_status).inc()
            except Exception:  # noqa: BLE001
                pass  # metrics are best-effort; never fail a task over them

    async def _run_task_inner(self, ws_socket: Any, assign: TaskAssign) -> None:
        executor: AgentRuntime
        agent_id_for_artifact: str | None = None

        if self._exec_override is not None:
            executor = self._exec_override
        elif assign.payload.agent == "shell":
            executor = ShellExecutor()
        elif assign.payload.agent == "claude-code":
            if not self._s.claude_code_bin:
                await ws_socket.send(TaskFailed(
                    task_id=assign.task_id,
                    reason="unsupported_agent",
                    detail="claude-code not configured on this runner",
                ).model_dump_json())
                return
            executor = ClaudeCodeExecutor(self._s.claude_code_bin)
        elif assign.payload.agent == "kiro-cli":
            if not self._s.kiro_cli_bin:
                await ws_socket.send(TaskFailed(
                    task_id=assign.task_id,
                    reason="unsupported_agent",
                    detail="kiro-cli not configured on this runner",
                ).model_dump_json())
                return
            executor = KiroCliExecutor(self._s.kiro_cli_bin)
        elif assign.payload.agent == "openclaw":
            if not self._s.openclaw_bin:
                await ws_socket.send(TaskFailed(
                    task_id=assign.task_id,
                    reason="unsupported_agent",
                    detail="openclaw not configured on this runner",
                ).model_dump_json())
                return
            from .agents.openclaw import OpenclawExecutor
            executor = OpenclawExecutor(
                self._s.openclaw_bin, agent_name=self._s.openclaw_agent_name,
            )
        elif assign.payload.agent == "hermes":
            if not self._s.hermes_bin:
                await ws_socket.send(TaskFailed(
                    task_id=assign.task_id,
                    reason="unsupported_agent",
                    detail="hermes not configured on this runner",
                ).model_dump_json())
                return
            from .agents.hermes import HermesExecutor
            executor = HermesExecutor(self._s.hermes_bin)
        elif assign.payload.agent == "codex":
            if not self._s.codex_bin:
                await ws_socket.send(TaskFailed(
                    task_id=assign.task_id,
                    reason="unsupported_agent",
                    detail="codex not configured on this runner",
                ).model_dump_json())
                return
            from .agents.codex import CodexExecutor
            executor = CodexExecutor(self._s.codex_bin)
        elif assign.payload.agent.startswith("agentcore-"):
            agent_id_for_artifact = assign.payload.agent.removeprefix("agentcore-")
            if not self._s.agentcore_enabled:
                await ws_socket.send(TaskFailed(
                    task_id=assign.task_id,
                    reason="unsupported_agent",
                    detail="agentcore not configured on this runner",
                ).model_dump_json())
                return
            if agent_id_for_artifact not in self._s.agentcore_agent_ids:
                await ws_socket.send(TaskFailed(
                    task_id=assign.task_id,
                    reason="unsupported_agent",
                    detail=f"agentcore agent {agent_id_for_artifact} not configured",
                ).model_dump_json())
                return
            from .agents.agentcore import AgentCoreExecutor
            executor = AgentCoreExecutor(
                agent_id=agent_id_for_artifact,
                region=self._s.agentcore_region,
                agent_alias_id=self._s.agentcore_agent_alias_id,
            )
        else:
            await ws_socket.send(TaskFailed(
                task_id=assign.task_id,
                reason="unknown_agent",
                detail=f"agent={assign.payload.agent}",
            ).model_dump_json())
            return

        await ws_socket.send(TaskStarted(task_id=assign.task_id).model_dump_json())
        exit_code: int | None = None
        stdout_tail: deque[str] = deque()
        stdout_tail_size = 0
        STDOUT_TAIL_CAP = 2048  # bytes — enough for a 500-char summary

        stdout_full_chunks: list[str] = []
        stdout_full_size = 0
        STDOUT_FULL_CAP = 64 * 1024  # for ccpt:ask detection
        # AgentCore tasks skip the git worktree flow — no repo needed.
        effective_repo = None if assign.payload.agent.startswith("agentcore-") else assign.payload.repo
        workspace = Workspace(
            work_dir=self._s.work_dir,
            workspace_slug=assign.payload.workspace_slug,
            task_id=assign.task_id,
            repo=effective_repo,
            base_branch=assign.payload.base_branch,
        )
        try:
            async with workspace as ws_ctx:
                # Context injection is only for AI agents; shell runs commands verbatim.
                ai_agent = assign.payload.agent != "shell"

                mem_block = ""
                dep_block = ""
                attach_block = ""
                if ai_agent:
                    if assign.payload.memory:
                        mem_block = render_memory(assign.payload.memory)
                        log.info(
                            "task %s: injected %d memory chunk(s)",
                            assign.task_id, len(assign.payload.memory),
                        )
                    if assign.payload.dependency_outputs:
                        dep_block = render_dependency_outputs(assign.payload.dependency_outputs)
                        log.info(
                            "task %s: injected dep-outputs for %d parent(s)",
                            assign.task_id, len(assign.payload.dependency_outputs),
                        )
                    if assign.payload.attachments:
                        # Pull the user-uploaded files into cwd before
                        # the agent starts so they can be Read/Bash'd
                        # natively. Phase 6 fail-loud: any download
                        # failure aborts the task with an explicit
                        # reason. Better than the old "agent runs
                        # without the file the user attached" silence.
                        try:
                            written = await download_attachments(
                                cwd=ws_ctx.cwd,
                                attachments=assign.payload.attachments,
                                core_http_url=self._s.core_http_url,
                                bearer_token=self._s.token,
                            )
                        except AttachmentError as ae:
                            log.warning(
                                "task %s: attachment download failed; "
                                "aborting task: %s",
                                assign.task_id, ae,
                            )
                            await ws_socket.send(TaskFailed(
                                task_id=assign.task_id,
                                reason="attachment_download_failed",
                                detail=str(ae),
                            ).model_dump_json())
                            return
                        attach_block = render_attachments_block(
                            cwd_relative_paths=written,
                            attachments=assign.payload.attachments,
                        )
                        log.info(
                            "task %s: downloaded %d/%d attachments",
                            assign.task_id,
                            len(written),
                            len(assign.payload.attachments),
                        )

                ctx = ws_ctx.collect_project_context() if ai_agent else None
                if ctx is not None:
                    ctx_path, ctx_text = ctx
                    ctx_block = (
                        f"<project-context source={ctx_path!r}>\n"
                        f"{ctx_text}\n"
                        f"</project-context>\n\n"
                    )
                    log.info(
                        "task %s: injected %d chars of project context from %s",
                        assign.task_id, len(ctx_text), ctx_path,
                    )
                else:
                    ctx_block = ""

                # Tell AI agents what's already wired up in the
                # environment, so they don't waste turns probing or
                # fall back to surfaces they can't actually use
                # (e.g. read-only deploy keys when they could just
                # call the GitHub API).
                cap_block = _build_capabilities_block() if ai_agent else ""

                # Order: memory (broadest) → dep-outputs (task-specific)
                #   → attachments (concrete inputs) → project ctx
                #   → capabilities → task
                if mem_block or dep_block or attach_block or ctx_block or cap_block:
                    final_prompt = (
                        mem_block
                        + dep_block
                        + attach_block
                        + ctx_block
                        + cap_block
                        + f"<task>\n{assign.payload.prompt}\n</task>"
                    )
                else:
                    final_prompt = assign.payload.prompt

                if assign.payload.prior_turns:
                    from .resume import build_resumed_prompt
                    final_prompt = build_resumed_prompt(
                        final_prompt,
                        assign.payload.prior_turns,
                    )

                try:
                    async with asyncio.timeout(_AGENT_RUN_HARD_TIMEOUT_SECONDS):
                        async for kind, data in executor.run(
                            task_id=assign.task_id,
                            prompt=final_prompt,
                            cwd=ws_ctx.cwd,
                        ):
                            if kind in {"stdout", "stderr"}:
                                seq = self._seq_by_task.get(assign.task_id, 0)
                                self._seq_by_task[assign.task_id] = seq + 1
                                await ws_socket.send(
                                    TaskLog(
                                        task_id=assign.task_id,
                                        seq=seq,
                                        stream=kind,  # type: ignore[arg-type]
                                        data=data,
                                    ).model_dump_json()
                                )
                                if kind == "stdout":
                                    stdout_tail.append(data)
                                    stdout_tail_size += len(data)
                                    while stdout_tail_size > STDOUT_TAIL_CAP and len(stdout_tail) > 1:
                                        dropped = stdout_tail.popleft()
                                        stdout_tail_size -= len(dropped)
                                    # Full-stdout buffer for ccpt:ask detection.
                                    stdout_full_chunks.append(data)
                                    stdout_full_size += len(data)
                                    while stdout_full_size > STDOUT_FULL_CAP and len(stdout_full_chunks) > 1:
                                        dropped_full = stdout_full_chunks.pop(0)
                                        stdout_full_size -= len(dropped_full)
                            elif kind == "finish":
                                exit_code = int(data)
                except TimeoutError:
                    # 12h hard cap hit. The executor's finally has
                    # already killed the subprocess. Surface as a
                    # specific TaskFailed reason so the kanban shows
                    # "ran past 12h" instead of a generic exception,
                    # and keep the worktree for postmortem.
                    workspace.keep()
                    log.warning(
                        "task %s exceeded %ds hard cap; killed agent",
                        assign.task_id, _AGENT_RUN_HARD_TIMEOUT_SECONDS,
                    )
                    await ws_socket.send(TaskFailed(
                        task_id=assign.task_id,
                        reason="hard_timeout",
                        detail=(
                            f"agent ran past the {_AGENT_RUN_HARD_TIMEOUT_SECONDS}s "
                            "runner-side hard cap and was killed; worktree kept for "
                            "postmortem"
                        ),
                    ).model_dump_json())
                    return
                except Exception:
                    workspace.keep()
                    raise
            # Outside `async with` — artifacts collected, worktree cleaned up if clean.
            # For AgentCore tasks, append the decision artifact (full model output) if available.
            from .agents.agentcore import AgentCoreExecutor
            if (
                agent_id_for_artifact is not None
                and isinstance(executor, AgentCoreExecutor)
                and executor.last_full_output
            ):
                workspace.add_artifact(ArtifactPayload(
                    kind="decision",
                    data=executor.last_full_output.encode("utf-8"),
                    meta={"agent_id": agent_id_for_artifact, "region": self._s.agentcore_region},
                ))
            artifacts = workspace.artifacts()
            log.info("task %s produced %d artifacts", assign.task_id, len(artifacts))
            await self._upload_artifacts(assign.task_id, artifacts)
        except Exception as e:  # noqa: BLE001
            await ws_socket.send(
                TaskFailed(
                    task_id=assign.task_id, reason="exception", detail=str(e)
                ).model_dump_json()
            )
            return

        assert exit_code is not None
        if exit_code == 0:
            from .ask_protocol import extract_ask

            stdout_full = "".join(stdout_full_chunks)
            question = extract_ask(stdout_full)

            if question:
                await ws_socket.send(
                    TaskAwaitingInput(
                        task_id=assign.task_id,
                        question=question,
                    ).model_dump_json()
                )
            else:
                summary: str | None = None
                override = getattr(executor, "summary", None)
                if callable(override):
                    try:
                        summary = override()
                    except Exception:  # noqa: BLE001 — never fail the task on summary extraction
                        log.warning("executor.summary() raised; falling back to stdout tail")
                        summary = None
                if not summary:
                    joined = "".join(stdout_tail).strip()
                    summary = joined[-500:] if joined else None
                await ws_socket.send(
                    TaskFinished(
                        task_id=assign.task_id, exit_code=0, summary=summary,
                    ).model_dump_json()
                )
        else:
            await ws_socket.send(
                TaskFailed(
                    task_id=assign.task_id,
                    reason="nonzero_exit",
                    detail=f"exit_code={exit_code}",
                ).model_dump_json()
            )

    async def _upload_artifacts(self, task_id: str, artifacts: list[ArtifactPayload]) -> None:
        if not artifacts:
            return
        async with httpx.AsyncClient() as client:
            for a in artifacts:
                headers = {
                    "Authorization": f"Bearer {self._s.token}",
                    "X-Task-ID": task_id,
                    "X-Artifact-Kind": a.kind,
                    "X-Artifact-Meta": json.dumps(a.meta) if a.meta else "",
                    "Content-Type": "application/octet-stream",
                }
                try:
                    r = await client.post(
                        f"{self._s.core_http_url}/api/v1/internal/artifacts",
                        headers=headers,
                        content=a.data,
                        timeout=30,
                    )
                    r.raise_for_status()
                    log.info(
                        "uploaded artifact %s for task %s (%d bytes)",
                        a.kind,
                        task_id,
                        len(a.data),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("artifact upload failed for %s/%s: %s", task_id, a.kind, e)


def _loads(raw: Any) -> dict:
    import json

    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8")
    return json.loads(raw)
