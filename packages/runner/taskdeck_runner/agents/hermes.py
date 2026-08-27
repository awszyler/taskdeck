"""Hermes Agent executor (https://github.com/nousresearch/hermes-agent).

Hermes is a CLI agent with tool-calling, MCP support, and skills. We
use the one-shot quiet-mode invocation:

    hermes chat -q "<prompt>" -Q

`-q` runs a single non-interactive query (no TTY chat loop).
`-Q` is quiet mode: suppresses banner, spinner, and tool previews,
emitting only the final response and a short trailing session_id line
on stderr. The yolo flag is intentionally NOT set — we trust the
sandbox boundary, not per-tool prompts (consistent with the other
runner agents post-P5.5; see runbook §16).

stdout: agent's final reply (sometimes preceded by setup warnings,
e.g. "no auxiliary LLM provider configured"). The runner already
truncates stdout to the last 500 chars for summary, which captures
the reply correctly even when there's preceding noise.

stderr: just `session_id: ...` typically.
"""
from __future__ import annotations

import asyncio
import logging
import re
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


class HermesExecutor:
    """Spawn `hermes chat -q "<prompt>" -Q`."""

    def __init__(self, bin_path: str):
        if not bin_path:
            raise ValueError("HermesExecutor requires a non-empty bin_path")
        self._bin = bin_path

    async def run(
        self, *, task_id: str, prompt: str, cwd: Path | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        kwargs: dict = {}
        if cwd is not None:
            kwargs["cwd"] = str(cwd)

        proc = await asyncio.create_subprocess_exec(
            self._bin, "chat", "-q", prompt, "-Q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT_BYTES,
            **kwargs,
        )

        # Capture the full stdout so we can write it to cwd/reply.md
        # at the end. Hermes (like openclaw) is a conversational agent
        # whose answer IS the deliverable for most tasks — without a
        # file in cwd, the kanban card has no "Open" output and the
        # user can only see a 500-char summary tail. reply.md fixes
        # that for any text-only agent reply.
        stdout_lines: list[str] = []

        async def pump(stream, label, accumulator: list[str] | None = None):
            assert stream is not None
            while True:
                line = await safe_readline(stream)
                if line is None:
                    continue
                if not line:
                    return
                # Strip ANSI defensively in case -Q misses some escape
                # sequences (e.g. when the underlying provider library
                # emits color codes we can't suppress at the CLI level).
                cleaned = _strip_ansi(line.decode(errors="replace")).rstrip("\n")
                if cleaned == "":
                    continue
                if accumulator is not None:
                    accumulator.append(cleaned)
                yield label, cleaned

        q: asyncio.Queue = asyncio.Queue()
        assert proc.stdout is not None
        assert proc.stderr is not None
        t1 = asyncio.create_task(drain_with_sentinel(
            pump(proc.stdout, "stdout", stdout_lines), q,
        ))
        t2 = asyncio.create_task(drain_with_sentinel(pump(proc.stderr, "stderr"), q))

        try:
            finished_pumps = 0
            while finished_pumps < 2:
                item = await q.get()
                if item is None:
                    finished_pumps += 1
                    continue
                yield item

            rc = await proc.wait()

            # Persist the agent's reply as a viewable document in cwd so
            # the kanban card surfaces it through the existing manifest
            # auto-detect (proto/output.py picks up *.md as kind=document).
            # Only on rc == 0 — error runs already get tail-stderr in the
            # task drawer.
            if cwd is not None and rc == 0 and stdout_lines:
                body = "\n".join(stdout_lines).strip()
                if body:
                    try:
                        (cwd / "reply.md").write_text(body, encoding="utf-8")
                    except OSError as e:
                        log.warning(
                            "hermes reply.md write failed in %s: %s", cwd, e,
                        )

            yield "finish", str(rc)
        finally:
            await kill_quietly(proc)
            await asyncio.gather(t1, t2, return_exceptions=True)
