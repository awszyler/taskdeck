"""kiro-cli executor.

Mirrors `claude_code.py`. Spawns:

    kiro-cli chat --no-interactive --trust-all-tools <prompt>

Strips ANSI escape sequences from stdout because kiro-cli emits color codes
by default and there is no `--no-color` flag exposed in the public CLI.
"""
from __future__ import annotations

import asyncio
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


class KiroCliExecutor:
    """Spawn `kiro-cli chat --no-interactive --trust-all-tools <prompt>`.

    Emits line-based ('stdout'|'stderr', str) tuples and a final
    ('finish', str(exit_code)). The runner process's environment is passed
    through, so AWS credentials / region / model preferences flow naturally
    via the ambient AWS configuration.
    """

    def __init__(self, bin_path: str):
        if not bin_path:
            raise ValueError("KiroCliExecutor requires a non-empty bin_path")
        self._bin = bin_path

    async def run(
        self, *, task_id: str, prompt: str, cwd: Path | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        kwargs: dict = {}
        if cwd is not None:
            kwargs["cwd"] = str(cwd)

        proc = await asyncio.create_subprocess_exec(
            self._bin, "chat", "--no-interactive", "--trust-all-tools", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT_BYTES,
            **kwargs,
        )

        async def pump(stream, label):
            assert stream is not None
            while True:
                line = await safe_readline(stream)
                if line is None:
                    continue
                if not line:
                    return
                cleaned = _strip_ansi(line.decode(errors="replace")).rstrip("\n")
                # ANSI cursor-control sequences sometimes leave behind empty lines;
                # drop them so logs stay readable.
                if cleaned == "":
                    continue
                yield label, cleaned

        q: asyncio.Queue = asyncio.Queue()
        assert proc.stdout is not None
        assert proc.stderr is not None
        t1 = asyncio.create_task(drain_with_sentinel(pump(proc.stdout, "stdout"), q))
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
            yield "finish", str(rc)
        finally:
            await kill_quietly(proc)
            await asyncio.gather(t1, t2, return_exceptions=True)
