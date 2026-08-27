"""OpenClaw executor — invokes the configured local OpenClaw agent.

OpenClaw (https://openclaw.ai) is a multi-channel AI agent CLI. We use
the one-shot embedded mode:

    openclaw agent --local --agent <name> --json \
        --session-id <task_id> --message "<prompt>"

It runs the agent in-process (no gateway daemon), produces a final JSON
result, and exits.

Quirks vs claude-code, and how we paper over them:

1. **Session-file lock contention on concurrent calls.** OpenClaw stores
   per-agent session state at ~/.openclaw/agents/<agent>/sessions/<sid>.jsonl
   with a sibling .lock file. When two `openclaw agent` invocations run
   in parallel against the same agent, they collide on the agent's
   active session lock — one of them fails after a 10s timeout with
   "session file locked".

   We tested whether `--session-id <unique>` would route each call to
   its own session file, but openclaw treats --session-id as a logical
   routing key that ultimately maps onto a small pool of jsonl files
   per agent — the lock is effectively per-agent, not per-sid.

   **The only reliable fix at the call layer is serialization.** We
   hold a process-wide asyncio.Lock (`_OPENCLAW_RUN_LOCK`) for the
   full lifetime of each openclaw invocation. This also eliminates a
   secondary race we'd otherwise own: if two concurrent runs both
   diff ~/.openclaw/workspace/ to capture their outputs, each could
   see the other's file writes and copy them as its own. Serializing
   makes the snapshot windows non-overlapping.

   The trade-off is no parallelism for openclaw tasks specifically
   (claude-code / hermes / kiro continue to run in parallel against
   their own worktrees). With parser routing now defaulting to
   claude-code, openclaw tasks are a minority and a 1-deep queue is
   acceptable.

   We still pass `--session-id <task_id>-<ts_ms>` for a different
   reason: each taskdeck /rerun is meant to be a clean slate (the
   kanban wipes prior logs and turns), and a fresh sid prevents the
   new run from inheriting the prior run's conversation history that
   the agent would otherwise see.

2. **stderr is enormous.** OpenClaw streams plugin registration logs,
   then dumps a multi-page JSON document containing its full system
   prompt report. stdout is empty. The executor buffers stderr,
   extracts the trailing JSON, and does NOT emit stderr as TaskLog
   events. Only the synthesized payload text reaches user-visible logs.

3. **Outputs land in OpenClaw's private workspace.** Files the agent
   produces (PPT, HTML, etc.) go to ~/.openclaw/workspace/, not the
   task cwd. We snapshot that directory before/after each run and
   copy any net-new / modified files into cwd so the kanban sees them.
   The full reply text is also written to cwd/reply.md so the card
   has at least one viewable output even when no files were produced.

4. **No ANSI noise** (unlike kiro-cli) — no stripping needed.

The binary path is injected; runner only constructs this executor when
TD_OPENCLAW_BIN is set.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ._subprocess_pump import (
    STREAM_LIMIT_BYTES,
    drain_with_sentinel,
    kill_quietly,
    safe_readline,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


log = logging.getLogger(__name__)


# Match a JSON object that starts on its own line (only `{`) — heuristic
# but reliable for openclaw's final-result format. Captures from `{` at
# line start to end-of-string.
_JSON_TAIL_RE = re.compile(r"^\{$.*\Z", re.MULTILINE | re.DOTALL)

# Process-wide lock that serializes every openclaw run. Necessary
# because:
# (a) openclaw's per-agent session file lock fails after a 10s timeout
#     when two invocations against the same agent run in parallel
#     (--session-id doesn't actually pin the underlying jsonl file in
#     a 1:1 way; we tested), and
# (b) our own pre/post workspace-mtime diff would mis-attribute one
#     task's file writes to a concurrently-running task without
#     serialization.
# Module-global rather than a per-executor field because the agent's
# session + workspace directory is a process-shared resource — separate
# OpenclawExecutor instances would still collide on it.
_OPENCLAW_RUN_LOCK = asyncio.Lock()


# OpenClaw produces files inside its own state directory, not the
# taskdeck task workspace. We snapshot before/after and copy net-new
# or modified files into the task cwd so they show up on the kanban
# card just like claude-code's git-tracked outputs do.
_OPENCLAW_WORKSPACE = Path(os.path.expanduser("~/.openclaw/workspace"))
# Skip the agent's own metadata files — they aren't task outputs.
_OPENCLAW_WORKSPACE_SKIP = frozenset({
    ".git", ".openclaw", "AGENTS.md", "BOOTSTRAP.md", "HEARTBEAT.md",
    "IDENTITY.md", "SOUL.md", "TOOLS.md", "USER.md", "memory",
})


def _extract_final_payloads(stderr_buf: str) -> list[str]:
    """Pull every agent reply text from the trailing JSON document.

    OpenClaw can emit multiple payloads per turn (e.g. an opening
    "starting work" line + a closing "delivered" line). Returning all
    of them lets the caller decide whether to use the last one as the
    summary or join them all for richer logs.
    """
    m = _JSON_TAIL_RE.search(stderr_buf)
    if m is None:
        return []
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    payloads = doc.get("payloads")
    if not isinstance(payloads, list):
        return []
    out: list[str] = []
    for p in payloads:
        if not isinstance(p, dict):
            continue
        text = p.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text)
    return out


def _snapshot_openclaw_workspace() -> dict[Path, float]:
    """Map of relative-path → mtime for every file in OpenClaw's
    workspace directory. Empty when the dir doesn't exist (e.g.
    fresh install). Used to diff before/after a task run.
    """
    if not _OPENCLAW_WORKSPACE.is_dir():
        return {}
    snapshot: dict[Path, float] = {}
    for p in _OPENCLAW_WORKSPACE.rglob("*"):
        try:
            if not p.is_file():
                continue
            rel = p.relative_to(_OPENCLAW_WORKSPACE)
        except (OSError, ValueError):
            continue
        # Skip openclaw's own metadata.
        if rel.parts and rel.parts[0] in _OPENCLAW_WORKSPACE_SKIP:
            continue
        try:
            snapshot[rel] = p.stat().st_mtime
        except OSError:
            continue
    return snapshot


def _diff_and_copy_outputs(
    pre: dict[Path, float], cwd: Path,
) -> list[Path]:
    """Find files that were created or modified after `pre` and copy
    them into `cwd`. Returns the list of relative paths copied.

    Strategy: rerun the snapshot, compare mtimes. We treat any file
    that's new OR has a strictly newer mtime as "this task's output".
    If nothing matches, return [] — runner will fall through to the
    existing manifest auto-detect, which still scans `cwd` for files
    the agent might have left there directly.
    """
    if not _OPENCLAW_WORKSPACE.is_dir():
        return []
    post = _snapshot_openclaw_workspace()
    new_files: list[Path] = []
    for rel, mtime in post.items():
        if rel in pre and pre[rel] >= mtime:
            continue
        src = _OPENCLAW_WORKSPACE / rel
        # Mirror openclaw's relative path under cwd. We flatten one
        # level so e.g. `subdir/foo.pptx` becomes `subdir/foo.pptx`,
        # but a top-level file stays at the top.
        dst = cwd / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            new_files.append(rel)
        except OSError as e:
            log.warning(
                "openclaw output copy failed %s -> %s: %s", src, dst, e,
            )
    return new_files


def _write_reply_doc(payloads: list[str], cwd: Path) -> None:
    """Write the agent's full conversational reply to reply.md in cwd
    so the kanban card always has at least one viewable output even
    when openclaw didn't produce any files.

    OpenClaw is conversational — its reply IS the deliverable for many
    tasks (translations, queries, summaries). Without this, those
    tasks show up on the board with no Open menu items even though
    the user got a useful answer in the chat channel."""
    if not payloads:
        return
    body = "\n\n".join(p.strip() for p in payloads if p.strip())
    if not body:
        return
    try:
        (cwd / "reply.md").write_text(body, encoding="utf-8")
    except OSError as e:
        log.warning("openclaw reply.md write failed in %s: %s", cwd, e)


# Backward-compat shim — preserve the old name some tests may use.
def _extract_final_text(stderr_buf: str) -> str | None:
    payloads = _extract_final_payloads(stderr_buf)
    return payloads[0] if payloads else None


class OpenclawExecutor:
    """Spawn `openclaw agent --local --agent <name> --json -m <prompt>`."""

    def __init__(self, bin_path: str, agent_name: str = "main"):
        if not bin_path:
            raise ValueError("OpenclawExecutor requires a non-empty bin_path")
        self._bin = bin_path
        self._agent_name = agent_name

    async def run(
        self, *, task_id: str, prompt: str, cwd: Path | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        # Serialize concurrent openclaw runs (see module docstring §1).
        # The lock is held for the full lifetime of this generator —
        # acquire on first invocation, release when the generator is
        # exhausted or its caller stops iterating.
        async with _OPENCLAW_RUN_LOCK:
            async for item in self._run_locked(
                task_id=task_id, prompt=prompt, cwd=cwd,
            ):
                yield item

    async def _run_locked(
        self, *, task_id: str, prompt: str, cwd: Path | None,
    ) -> AsyncIterator[tuple[str, str]]:
        kwargs: dict = {}
        if cwd is not None:
            kwargs["cwd"] = str(cwd)

        # Snapshot openclaw's own workspace before the run. After the
        # agent finishes we diff this against the post-state and copy
        # any net-new / modified files into `cwd`. Without this, files
        # the agent generates (PPT, HTML, etc.) live in
        # ~/.openclaw/workspace/ where neither the kanban nor sandbox
        # would ever look. The diff is race-free because of the
        # process-wide _OPENCLAW_RUN_LOCK held by run().
        pre_snapshot = _snapshot_openclaw_workspace()

        # task_id alone would reuse the same session on rerun, which
        # would inherit the prior run's conversation history — opposite
        # of the kanban /rerun "clean slate" semantics (logs and turns
        # are wiped). Append a millisecond timestamp so each run gets
        # a fresh openclaw session while still being deterministic
        # within a single run.
        session_id = f"{task_id}-{int(time.time() * 1000)}"

        proc = await asyncio.create_subprocess_exec(
            self._bin, "agent", "--local",
            "--agent", self._agent_name,
            # --session-id keeps each rerun from inheriting the prior
            # run's conversation history. The kanban /rerun wipes
            # turns and logs; passing a fresh sid here keeps the
            # agent side consistent with that "clean slate" semantic.
            # (Concurrency safety comes from _OPENCLAW_RUN_LOCK above,
            # not from sid uniqueness.)
            "--session-id", session_id,
            "--json",
            "--message", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT_BYTES,
            **kwargs,
        )

        # stderr is buffered locally but NOT emitted as TaskLog events
        # (see module docstring). Only the synthesized final-text line
        # reaches the user-visible logs on success. On non-zero exit we
        # dump a tail of stderr as a fallback for postmortem.
        stderr_buf: list[str] = []

        async def pump_stdout(stream):
            assert stream is not None
            while True:
                line = await safe_readline(stream)
                if line is None:
                    continue
                if not line:
                    return
                yield "stdout", line.decode(errors="replace").rstrip("\n")

        async def consume_stderr(stream):
            """Drain stderr into the local buffer; never yield."""
            assert stream is not None
            while True:
                line = await safe_readline(stream)
                if line is None:
                    continue
                if not line:
                    return
                stderr_buf.append(line.decode(errors="replace"))

        q: asyncio.Queue = asyncio.Queue()
        assert proc.stdout is not None
        assert proc.stderr is not None
        t1 = asyncio.create_task(drain_with_sentinel(pump_stdout(proc.stdout), q))
        # stderr drain runs in parallel but does not feed the queue.
        t2 = asyncio.create_task(consume_stderr(proc.stderr))

        try:
            # Only one pump (stdout) feeds the queue.
            while True:
                item = await q.get()
                if item is None:
                    break
                yield item

            rc = await proc.wait()

            # On non-zero exit, surface the tail of stderr so the user can
            # see what actually broke. 20 lines is enough for tracebacks
            # and openclaw's typical error reporting.
            if rc != 0:
                tail = stderr_buf[-20:] if len(stderr_buf) > 20 else stderr_buf
                for line in tail:
                    yield "stderr", line.rstrip("\n")

            # Extract every payload from buffered stderr. Use the LAST one
            # as the line that becomes task.summary (typically the closing
            # "delivered" message), and write the joined text to reply.md
            # in cwd so the kanban card has at least one document output
            # even when the agent didn't produce files.
            all_payloads = _extract_final_payloads("".join(stderr_buf))
            if all_payloads:
                yield "stdout", all_payloads[-1]
                if cwd is not None and rc == 0:
                    _write_reply_doc(all_payloads, cwd)

            # Copy any files openclaw produced in its private workspace
            # into the task cwd. Always runs (even on rc != 0) so partial
            # output is still recoverable. List the copied files for the
            # user-visible logs so they know where to find them.
            if cwd is not None:
                copied = _diff_and_copy_outputs(pre_snapshot, cwd)
                if copied:
                    yield "stdout", (
                        "OpenClaw produced files: "
                        + ", ".join(str(p) for p in copied)
                    )

            yield "finish", str(rc)
        finally:
            await kill_quietly(proc)
            await asyncio.gather(t1, t2, return_exceptions=True)
