"""Tests for workspace LRU GC (P6.3.3)."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from sandbox_host.settings import SandboxHostSettings
from sandbox_host.state import SandboxRecord, SandboxRegistry
from sandbox_host.workspace_gc import (
    _deepest_mtime,
    reclaim_stale_worktrees,
)


def _settings_for(work_dir, retention_days=30):
    return SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR=str(work_dir),
        TD_SBH_CONTAINER_RUNTIME="runc",
        TD_SBH_WORKSPACE_RETENTION_DAYS=retention_days,
    )


def _seed_workspace(work_dir, slug, task_id, mtime: datetime | None = None):
    """Create <work_dir>/<slug>/tasks/<task_id>/ with a file inside,
    optionally backdate its mtime."""
    task_dir = work_dir / slug / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    f = task_dir / "marker.txt"
    f.write_text("hi")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(f, (ts, ts))
        os.utime(task_dir, (ts, ts))
    return task_dir


@pytest.mark.asyncio
async def test_gc_skips_fresh_workspaces(tmp_path):
    work_dir = tmp_path / "work"
    fresh = _seed_workspace(work_dir, "ws-a", "task-fresh")  # mtime = now
    settings = _settings_for(work_dir)
    registry = SandboxRegistry(db_path=tmp_path / "state.db")

    deleted = await reclaim_stale_worktrees(settings=settings, registry=registry)
    assert deleted == 0
    assert fresh.exists()


@pytest.mark.asyncio
async def test_gc_prunes_old_workspaces(tmp_path):
    work_dir = tmp_path / "work"
    old_time = datetime.now(UTC) - timedelta(days=45)
    stale = _seed_workspace(work_dir, "ws-b", "task-stale", mtime=old_time)
    fresh = _seed_workspace(work_dir, "ws-b", "task-fresh")
    settings = _settings_for(work_dir, retention_days=30)
    registry = SandboxRegistry(db_path=tmp_path / "state.db")

    deleted = await reclaim_stale_worktrees(settings=settings, registry=registry)
    assert deleted == 1
    assert not stale.exists()
    assert fresh.exists()


@pytest.mark.asyncio
async def test_gc_skips_running_sandboxes(tmp_path):
    """Even an old worktree must NOT be deleted while its sandbox is
    running (registry has a record for it)."""
    work_dir = tmp_path / "work"
    old_time = datetime.now(UTC) - timedelta(days=45)
    running = _seed_workspace(work_dir, "ws-c", "running-task", mtime=old_time)

    settings = _settings_for(work_dir, retention_days=30)
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    now = datetime.now(UTC)
    await registry.add(SandboxRecord(
        task_id="running-task",
        container_id="cid", container_name="x", network_name="x",
        host_port=8080, internal_port=8080, runtime="static",
        image="x", base_path="/", started_at=now, last_request_at=now,
    ))

    deleted = await reclaim_stale_worktrees(settings=settings, registry=registry)
    assert deleted == 0
    assert running.exists()


@pytest.mark.asyncio
async def test_gc_handles_missing_work_dir(tmp_path):
    settings = _settings_for(tmp_path / "does-not-exist")
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    deleted = await reclaim_stale_worktrees(settings=settings, registry=registry)
    assert deleted == 0


def test_deepest_mtime_finds_newest_descendant(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    f1 = d / "a.txt"
    f1.write_text("hi")
    f2 = d / "sub" / "b.txt"
    f2.parent.mkdir()
    f2.write_text("hi")

    # Backdate dir + f1, leave f2 fresh.
    old = (datetime.now(UTC) - timedelta(days=10)).timestamp()
    os.utime(d, (old, old))
    os.utime(f1, (old, old))

    latest = _deepest_mtime(d)
    # Should reflect f2's mtime (now), not the older dir/f1.
    assert (datetime.now(UTC) - latest).total_seconds() < 5
