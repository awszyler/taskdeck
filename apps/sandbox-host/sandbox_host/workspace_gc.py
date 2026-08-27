"""Workspace retention LRU GC (P6.3.3).

Runs in sandbox-host because it owns the host filesystem layout for
sandboxes. The runner used to delete worktrees on clean exit; we
removed that so users can launch sandboxes against any past task.
This module is the cleanup half: every workspace_gc_interval_seconds
we walk <work_dir>/<slug>/tasks/<task_id>/ and rmtree anything whose
most-recent file mtime is older than workspace_retention_days.

Why mtime-of-most-recent-file rather than dir mtime: dir mtime
changes when files are added/removed but not when they're read or
modified, so a quiescent dir's mtime can be older than its contents.
We sample the deepest mtime to be safe.

Side-effect-free for non-existent / empty work_dir: the GC is a
no-op for those.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .archive import archive_workspace
from .settings import SandboxHostSettings
from .state import SandboxRegistry

log = logging.getLogger(__name__)


def _deepest_mtime(path: Path) -> datetime:
    """Return the most recent mtime across path itself and all
    descendants. Returns the path's own mtime if empty."""
    try:
        latest = path.stat().st_mtime
    except OSError:
        return datetime.now(UTC)  # treat unreadable as fresh — be safe
    for sub in path.rglob("*"):
        try:
            m = sub.stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            continue
    return datetime.fromtimestamp(latest, tz=UTC)


async def reclaim_stale_worktrees(
    *,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
) -> int:
    """One pass over <work_dir>/<slug>/tasks/<task_id>/ pruning
    anything older than retention_days. Returns the number of
    worktrees deleted."""
    threshold = timedelta(days=settings.workspace_retention_days)
    cutoff = datetime.now(UTC) - threshold
    work_dir = settings.work_dir

    if not work_dir.is_dir():
        return 0

    # Build set of currently-running task_ids so we never delete an
    # active sandbox's mount source.
    active_task_ids: set[str] = {
        rec.task_id for rec in await registry.list_all()
    }

    deleted = 0
    for slug_dir in work_dir.iterdir():
        if not slug_dir.is_dir():
            continue
        tasks_dir = slug_dir / "tasks"
        if not tasks_dir.is_dir():
            continue

        for task_dir in tasks_dir.iterdir():
            if not task_dir.is_dir():
                continue
            task_id = task_dir.name
            if task_id in active_task_ids:
                continue
            try:
                age_check = _deepest_mtime(task_dir)
            except Exception as e:  # noqa: BLE001
                log.warning("mtime read failed for %s: %s", task_dir, e)
                continue
            if age_check < cutoff:
                # P6.3.7 archive: tar.gz to S3 (and notify core) before
                # rm. Best-effort — on any archive failure we still rm
                # so the workspace doesn't outlive its retention. The
                # user just won't be able to reactivate that one task.
                slug = slug_dir.name
                if settings.archive_bucket:
                    await archive_workspace(
                        settings=settings,
                        workspace_slug=slug,
                        task_id=task_id,
                        workspace_dir=task_dir,
                    )
                try:
                    shutil.rmtree(task_dir)
                    deleted += 1
                    log.info(
                        "pruned stale worktree %s (mtime=%s, cutoff=%s)",
                        task_dir, age_check.isoformat(), cutoff.isoformat(),
                    )
                except OSError as e:
                    log.warning("rmtree(%s) failed: %s", task_dir, e)

    return deleted


async def workspace_gc_loop(
    *,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    stop_event: asyncio.Event,
) -> None:
    """Long-running task. Polls workspace_gc_interval_seconds,
    exits cleanly on stop_event."""
    interval = settings.workspace_gc_interval_seconds
    log.info(
        "workspace gc loop starting (interval=%ds, retention=%dd)",
        interval, settings.workspace_retention_days,
    )
    while not stop_event.is_set():
        try:
            await reclaim_stale_worktrees(settings=settings, registry=registry)
        except Exception as e:  # noqa: BLE001
            log.warning("workspace gc iteration error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
    log.info("workspace gc loop exiting")
