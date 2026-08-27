"""Codex CLI executor (https://github.com/openai/codex).

Headless invocation:

    codex exec --json --skip-git-repo-check \\
        --dangerously-bypass-approvals-and-sandbox \\
        --cd <cwd> \\
        -o <cwd>/.codex-last-message.txt \\
        <prompt>

`exec` is the non-interactive subcommand (interactive TUI is the default
when no subcommand is given — wrong shape for a runner).
`--json` emits one JSONL event per line on stdout. Event types we care
about:

- `thread.started`: swallowed (boilerplate)
- `turn.started`:   swallowed (boilerplate)
- `item.started`  + `item.completed`:
    - `type: "command_execution"`: the agent's bash invocations.
      We render each completed call as a single tool-style line
      `[tool: bash] cmd: <command>` so the drawer log shows a
      readable trace instead of a wall of brackets.
    - `type: "agent_message"`: model narrative text. Surfaced
      verbatim. The LAST agent_message becomes the kanban summary.
    - other types (file_change, todo_update, ...): rendered as
      `[item: <type>]` so they're visible without flooding the log.
- `turn.completed`: swallowed; we already captured the last
  agent_message and `-o` wrote it to disk for cross-checking.

`--skip-git-repo-check` lets codex run inside a worktree dir that may
not be a top-level git repo (it sometimes is via the workspace
manager, sometimes isn't — codex insists on a flag rather than
auto-detecting).

`--dangerously-bypass-approvals-and-sandbox` matches the trust model
of the other runner agents: trust is enforced at the L2 worktree /
container boundary, not per-tool prompts. See runbook §16.

`-o <file>`: codex writes its final user-facing message to this file
on success. We read it after the process exits as the authoritative
summary text. Per-task-cwd path so two parallel codex runs don't
clobber each other's output.

stdin is closed (`< /dev/null` equivalent via PIPE + immediate close)
because `codex exec` otherwise prints "Reading additional input from
stdin..." on stderr and waits for stdin EOF before processing the
positional prompt — even when a prompt was already given. Closing
stdin removes both behaviors.
"""
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

_LAST_MESSAGE_FILE = ".codex-last-message.txt"


class CodexExecutor:
    """Spawn `codex exec --json ... <prompt>`."""

    def __init__(self, bin_path: str):
        if not bin_path:
            raise ValueError("CodexExecutor requires a non-empty bin_path")
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

        # Per-task-cwd output path — falls back to None when cwd is
        # unset (only happens in unit tests with a fake executor).
        last_msg_path = (cwd / _LAST_MESSAGE_FILE) if cwd is not None else None

        argv = [
            self._bin, "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if cwd is not None:
            argv += ["--cd", str(cwd)]
        if last_msg_path is not None:
            argv += ["-o", str(last_msg_path)]
        argv.append(prompt)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT_BYTES,
            **kwargs,
        )
        # Close stdin immediately so codex doesn't sit on the
        # "Reading additional input from stdin..." path.
        if proc.stdin is not None:
            proc.stdin.close()

        async def pump_stdout(stream):
            """Parse each JSONL event and surface it as readable stdout."""
            assert stream is not None
            while True:
                line = await safe_readline(stream)
                if line is None:
                    # Single line over the readline limit. Drop and
                    # continue — typical cause is a tool result with
                    # large stdout, which wouldn't render anything
                    # user-visible from the JSON wrapper anyway.
                    continue
                if not line:
                    return
                raw = line.decode(errors="replace").rstrip("\n")
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
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
                text = line.decode(errors="replace").rstrip("\n")
                # Drop the "Reading additional input from stdin..."
                # boilerplate codex emits even with stdin closed —
                # it's noise, not a real warning.
                if not text or text.startswith("Reading additional input"):
                    continue
                yield "stderr", text

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

            # If JSONL parsing didn't capture an agent_message (rare —
            # e.g. codex exited mid-turn), fall back to the file
            # codex wrote via `-o`. Either path produces the same
            # summary; we just want belt-and-suspenders.
            if (
                self._summary is None
                and last_msg_path is not None
                and rc == 0
            ):
                try:
                    text = last_msg_path.read_text(encoding="utf-8").strip()
                    if text:
                        self._summary = _trim_for_summary(text)
                except OSError:
                    pass

            yield "finish", str(rc)
        finally:
            await kill_quietly(proc)
            await asyncio.gather(t1, t2, return_exceptions=True)

    async def _render_event(self, ev: dict) -> AsyncIterator[tuple[str, str]]:
        ev_type = ev.get("type")

        # Only render on item.completed — `item.started` for the same
        # id will be followed by a matching completed event with the
        # final state (exit_code, full text, etc.). Rendering both
        # would double the log noise.
        if ev_type != "item.completed":
            return

        item = ev.get("item")
        if not isinstance(item, dict):
            return

        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                # Capture the LAST agent_message as the summary; codex
                # may emit multiple within a turn (rare), and the
                # closing one is the deliverable.
                self._summary = _trim_for_summary(text.strip())
                yield "stdout", text
            return

        if item_type == "command_execution":
            cmd = item.get("command")
            if isinstance(cmd, str) and cmd:
                # Hint includes exit code so failed tool calls are
                # visible in the drawer log without expanding the
                # full event.
                exit_code = item.get("exit_code")
                suffix = ""
                if isinstance(exit_code, int) and exit_code != 0:
                    suffix = f" (exit {exit_code})"
                yield "stdout", f"[tool: bash] cmd={cmd[:240]}{suffix}"
            return

        # Other item kinds (file_change, todo_update, mcp_tool_call,
        # ...). Render a single line so the user sees that something
        # happened, without dumping the full JSON.
        yield "stdout", f"[item: {item_type}]"


def _trim_for_summary(text: str) -> str:
    """Match the 480-char cap claude-code uses so all agents render
    consistently in the kanban summary column."""
    if len(text) > 480:
        return text[:480].rstrip() + "…"
    return text
