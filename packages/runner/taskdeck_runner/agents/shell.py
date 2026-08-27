from __future__ import annotations

import asyncio
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


class ShellExecutor:
    """M1 executor: runs the task prompt as a shell command.

    Yields tuples: ('stdout'|'stderr', str) or ('finish', '<exit_code>').
    """

    async def run(
        self, *, task_id: str, prompt: str, cwd: Path | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        kwargs = {}
        if cwd is not None:
            kwargs["cwd"] = str(cwd)
        proc = await asyncio.create_subprocess_shell(
            prompt,
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
                yield label, line.decode(errors="replace").rstrip("\n")

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
