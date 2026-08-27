"""Shared helpers for agent subprocess stdout/stderr pumping.

History note: a single >64 KB line on stdout (claude-code stream-json
emitting a `tool_result` containing a base64-encoded image) used to
deadlock the runner permanently. asyncio's StreamReader defaults to
``_DEFAULT_LIMIT == 64 KiB``; ``readline()`` raised ``LimitOverrunError``
inside the per-agent pump task, the exception escaped without setting a
sentinel on the queue, and the consumer ``await q.get()`` blocked forever.
This module exists so every agent goes through the same fail-loud pump,
and the lesson stays codified instead of having to be re-learned.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# 4 MiB. Comfortably covers the largest legitimate stream-json line
# (~1080p PNG via Read tool: ~4-6 MB raw, ~6-8 MB base64). For agents
# that emit pathological output beyond this, the safe_readline drop-
# and-resync below keeps the pump alive instead of exploding.
STREAM_LIMIT_BYTES = 4 * 1024 * 1024


async def safe_readline(stream: asyncio.StreamReader) -> bytes | None:
    """Read one line from a subprocess stream, surviving over-limit lines.

    Returns:
      - bytes (with trailing newline) on a normal line
      - empty bytes ``b""`` on EOF
      - ``None`` on an over-limit line that we dropped — caller should
        treat it like a normal line (skip + continue), but may want to
        log it.

    asyncio's ``readline()`` raises ``LimitOverrunError`` (subclass of
    ``ValueError``) when a line exceeds the StreamReader limit. The
    documented recovery is to drain the offending data via ``read()``
    or ``readuntil()`` and continue. We do that here so pump tasks
    never die on a single bloated line.
    """
    try:
        return await stream.readline()
    except asyncio.LimitOverrunError as e:
        # Drain the overflowing chunk in pieces. ``e.consumed`` is the
        # offset of the separator we'd have stopped at; reading that
        # many bytes consumes the line including its terminator on a
        # subsequent fetch. Loop in case the buffer keeps overflowing
        # before we hit a newline (very long line with multiple
        # internal-buffer flushes).
        try:
            await stream.readexactly(e.consumed)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            # Stream ended or buffer is still saturated. Try once
            # more via readuntil to land on the next \n; on failure
            # we bail out and let the caller see EOF.
            try:
                await stream.readuntil(b"\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                return b""
        return None


async def kill_quietly(proc: asyncio.subprocess.Process) -> None:
    """Best-effort terminate a subprocess. Used in finally blocks so a
    cancelled / timed-out run() never leaks a zombie agent process.

    SIGTERM first (gives the agent CLI a chance to flush its session
    state), then SIGKILL after 5s if it hasn't exited — agent CLIs that
    ignore SIGTERM exist (kiro-cli has been seen to). Returns when the
    process is reaped or both attempts have been issued.
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
        return
    except TimeoutError:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    # Reaper may still fail after SIGKILL (rare; usually means the pid
    # is stuck in uninterruptible sleep). We've done what we can — the
    # caller surfaces this as TaskFailed regardless.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=5)


async def drain_with_sentinel(
    src_gen: AsyncGenerator[Any, None], queue: asyncio.Queue
) -> None:
    """Forward items from an async generator into a queue, guaranteeing
    a ``None`` sentinel is enqueued exactly once when the generator ends
    — whether it returns cleanly or raises.

    Without the ``finally``, an exception in the source generator
    would skip ``put(None)`` and any consumer doing
    ``while finished < N: q.get()`` would hang forever. That's the
    deadlock that bit task 90207737 — see module docstring.
    """
    try:
        async for item in src_gen:
            await queue.put(item)
    finally:
        await queue.put(None)
