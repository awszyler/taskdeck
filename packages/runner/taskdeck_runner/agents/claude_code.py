from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from ._subprocess_pump import (
    STREAM_LIMIT_BYTES,
    drain_with_sentinel,
    kill_quietly,
    safe_readline,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


log = logging.getLogger(__name__)


class ClaudeCodeExecutor:
    """Spawn `claude --print --output-format stream-json <prompt>`.

    stream-json emits one JSON event per line:
      - `type: "system"` (init / hook lifecycle): swallowed, never shown
      - `type: "assistant"` (agent message): text blocks are surfaced as
        readable stdout lines so the drawer log shows what the agent
        actually said. tool_use blocks are summarised on a single line
        like "[tool: Bash] cmd: ..." rather than dumping their full
        argument JSON, since the latter ends up unreadable in the
        drawer log view.
      - `type: "result"` (always last): the `result` string is the
        agent's final answer text — exactly what users want as the
        kanban-card summary. Captured into `self._summary`, exposed
        via the `summary()` callable runner crp_client prefers over
        the raw stdout-tail fallback.

    Without stream-json + this parser, summary() fell back to the last
    500 bytes of plain stdout, which on long tool-using runs is the
    tail of intermediate verification output (per-slide stat tables
    etc.) — not the agent's narrative wrap-up.

    The binary path is injected; runner calls this only when the path is
    configured. Environment of the runner process is passed through, so
    ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY / LITELLM_* etc. flow naturally.
    """

    def __init__(self, bin_path: str):
        if not bin_path:
            raise ValueError("ClaudeCodeExecutor requires a non-empty bin_path")
        self._bin = bin_path
        self._summary: str | None = None

    def summary(self) -> str | None:
        return self._summary

    async def run(
        self, *, task_id: str, prompt: str, cwd: Path | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        kwargs: dict = {}
        if cwd is not None:
            kwargs["cwd"] = str(cwd)

        # --print: non-interactive mode. Exit on completion.
        # --output-format stream-json + --verbose: structured per-event
        # output we can parse for summary. (Plain --output-format text
        # would mix narrative + tool output in stdout, making the
        # last-500-bytes summary fallback meaningless on long runs.)
        # --permission-mode bypassPermissions: matches kiro-cli's --trust-all-tools.
        # Headless runner has no IDE to surface CLI permission prompts; trust is
        # enforced at the container/worktree boundary (L2), not per tool call.
        # See runbook §16 for the security model and follow-ups.
        proc = await asyncio.create_subprocess_exec(
            self._bin, "--print",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT_BYTES,
            **kwargs,
        )

        async def pump_stdout(stream):
            """Parse each JSON line and surface it as readable stdout."""
            assert stream is not None
            while True:
                line = await safe_readline(stream)
                if line is None:
                    # Single line over the readline limit — already
                    # drained by safe_readline. Drop and continue;
                    # tool_result events with huge base64 payloads
                    # don't render anything user-visible anyway.
                    continue
                if not line:
                    return
                raw = line.decode(errors="replace").rstrip("\n")
                if not raw:
                    continue
                # Try to parse as a stream-json event.
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    # Non-JSON line — surface verbatim (rare, but
                    # claude can emit a couple of plain warnings
                    # before stream-json kicks in).
                    yield "stdout", raw
                    continue
                async for item in self._render_event(ev):
                    yield item

        async def pump_stderr(stream):
            assert stream is not None
            while True:
                line = await safe_readline(stream)
                if line is None:
                    continue
                if not line:
                    return
                yield "stderr", line.decode(errors="replace").rstrip("\n")

        q: asyncio.Queue = asyncio.Queue()
        assert proc.stdout is not None
        assert proc.stderr is not None
        t1 = asyncio.create_task(drain_with_sentinel(pump_stdout(proc.stdout), q))
        t2 = asyncio.create_task(drain_with_sentinel(pump_stderr(proc.stderr), q))

        try:
            finished_pumps = 0
            while finished_pumps < 2:
                item = await q.get()
                if item is None:
                    finished_pumps += 1
                    continue
                yield item

            rc = await proc.wait()
            yield "finish", str(rc)
        finally:
            # Cancel/timeout path: kill the agent so we don't leak a
            # claude subprocess (and the LLM tokens it would burn) past
            # the runner's hard wall-clock cap. No-op when run()
            # completed cleanly above.
            await kill_quietly(proc)
            await asyncio.gather(t1, t2, return_exceptions=True)

    async def _render_event(self, ev: dict) -> AsyncIterator[tuple[str, str]]:
        ev_type = ev.get("type")
        if ev_type == "result":
            # Final answer — capture for summary(). Trim because the
            # kanban summary column is 500 chars and the model can be
            # chatty.
            result_text = ev.get("result")
            if isinstance(result_text, str) and result_text.strip():
                trimmed = result_text.strip()
                if len(trimmed) > 480:
                    trimmed = trimmed[:480].rstrip() + "…"
                self._summary = trimmed
                # Also surface the final answer as a stdout line so
                # the task drawer's log view ends with the narrative
                # rather than a tool-use trace.
                yield "stdout", result_text
            return
        if ev_type == "assistant":
            msg = ev.get("message")
            if not isinstance(msg, dict):
                return
            for block in msg.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        yield "stdout", text
                elif btype == "tool_use":
                    tool_name = block.get("name", "?")
                    # Best-effort short summary of the call: input_text
                    # field if present (many tools), else the smallest
                    # input value as a one-liner. Avoid dumping the
                    # whole input JSON — that's what made the old log
                    # view a wall of brackets.
                    inp = block.get("input")
                    arg_hint = ""
                    if isinstance(inp, dict):
                        for key in ("command", "cmd", "path", "file_path",
                                     "query", "pattern", "url"):
                            v = inp.get(key)
                            if isinstance(v, str) and v:
                                arg_hint = f" {key}={v[:120]}"
                                break
                    yield "stdout", f"[tool: {tool_name}]{arg_hint}"
            return
        # system / hook / partial — silently discard.
        return
