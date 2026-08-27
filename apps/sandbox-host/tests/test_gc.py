"""Tests for idle GC + startup orphan cleanup."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from docker.errors import NotFound
from sandbox_host.gc import cleanup_orphans_at_startup, gc_loop, reclaim_idle
from sandbox_host.settings import SandboxHostSettings
from sandbox_host.state import SandboxRecord, SandboxRegistry


@pytest.fixture
def settings():
    return SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR="/tmp/td-test",
        TD_SBH_CONTAINER_RUNTIME="runc",
        TD_SBH_IDLE_SECONDS=60,
        TD_SBH_GC_INTERVAL=1,
    )


def _record(task_id: str, idle_seconds: int) -> SandboxRecord:
    """Build a record whose last_request_at is `idle_seconds` ago."""
    started = datetime.now(UTC) - timedelta(seconds=idle_seconds)
    return SandboxRecord(
        task_id=task_id,
        container_id="cid",
        container_name=f"td-sandbox-{task_id}",
        network_name=f"td-sandbox-net-{task_id}",
        host_port=10000,
        internal_port=8080,
        runtime="static",
        image="x",
        base_path=f"/sandbox/{task_id}/",
        started_at=started,
        last_request_at=started,
        status="running",
    )


@pytest.mark.asyncio
async def test_reclaim_idle_removes_only_idle(settings, tmp_path):
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    await registry.add(_record("idle", idle_seconds=120))   # over threshold
    await registry.add(_record("active", idle_seconds=10))  # under threshold

    docker_client = MagicMock()
    docker_client.containers.get = MagicMock(side_effect=NotFound("none"))
    docker_client.networks.get = MagicMock(side_effect=NotFound("none"))

    count = await reclaim_idle(
        settings=settings, registry=registry, docker_client=docker_client,
    )
    assert count == 1
    assert await registry.get("idle") is None
    assert await registry.get("active") is not None


@pytest.mark.asyncio
async def test_reclaim_idle_threshold_inclusive(settings, tmp_path):
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    # Exactly at threshold should reclaim (>= comparison).
    await registry.add(_record("at-threshold", idle_seconds=60))

    docker_client = MagicMock()
    docker_client.containers.get = MagicMock(side_effect=NotFound("none"))
    docker_client.networks.get = MagicMock(side_effect=NotFound("none"))

    count = await reclaim_idle(
        settings=settings, registry=registry, docker_client=docker_client,
    )
    assert count == 1


@pytest.mark.asyncio
async def test_reclaim_idle_handles_teardown_failure(settings, tmp_path):
    """A teardown that throws shouldn't break the loop."""
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    await registry.add(_record("a", idle_seconds=120))
    await registry.add(_record("b", idle_seconds=120))

    # Make docker raise — teardown's _best_effort_cleanup swallows it,
    # so reclaim_idle still completes for both.
    docker_client = MagicMock()
    docker_client.containers.get = MagicMock(side_effect=Exception("boom"))
    docker_client.networks.get = MagicMock(side_effect=NotFound("none"))

    count = await reclaim_idle(
        settings=settings, registry=registry, docker_client=docker_client,
    )
    # Both attempted (count is the number we *wanted* to reclaim).
    assert count == 2


@pytest.mark.asyncio
async def test_gc_loop_exits_on_stop_event(settings, tmp_path):
    """The loop should respond to stop_event quickly even between ticks."""
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    stop = asyncio.Event()

    task = asyncio.create_task(
        gc_loop(settings=settings, registry=registry, stop_event=stop),
    )
    # Let the loop run one iteration.
    await asyncio.sleep(0.1)
    stop.set()
    # Should exit promptly (not wait for the full interval).
    await asyncio.wait_for(task, timeout=2)


# ---------- startup orphan cleanup -------------------------------------


@pytest.mark.asyncio
async def test_cleanup_orphans_removes_labeled_containers(settings):
    docker_client = MagicMock()

    orphan_a = MagicMock()
    orphan_a.name = "td-sandbox-stale-a"
    orphan_a.labels = {"ccpt.sandbox.task_id": "stale-a"}

    orphan_b = MagicMock()
    orphan_b.name = "td-sandbox-stale-b"
    orphan_b.labels = {"ccpt.sandbox.task_id": "stale-b"}

    docker_client.containers.list = MagicMock(return_value=[orphan_a, orphan_b])
    # Per-orphan cleanup paths.
    docker_client.containers.get = MagicMock(side_effect=NotFound("ok-cleaned"))
    docker_client.networks.get = MagicMock(side_effect=NotFound("ok-cleaned"))
    docker_client.networks.list = MagicMock(return_value=[])

    removed = await cleanup_orphans_at_startup(
        settings=settings, docker_client=docker_client,
    )
    assert removed == 2
    docker_client.containers.list.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_orphans_removes_zombie_networks(settings):
    docker_client = MagicMock()
    docker_client.containers.list = MagicMock(return_value=[])

    zombie = MagicMock()
    zombie.name = "td-sandbox-net-zombie"
    zombie.attrs = {"Containers": {}}
    zombie.reload = MagicMock()
    zombie.remove = MagicMock()

    other_net = MagicMock()
    other_net.name = "some-unrelated-network"

    docker_client.networks.list = MagicMock(return_value=[zombie, other_net])

    await cleanup_orphans_at_startup(
        settings=settings, docker_client=docker_client,
    )
    # The td-sandbox-net-* one was removed; the unrelated one was not.
    zombie.remove.assert_called_once()
    other_net.remove = getattr(other_net, "remove", MagicMock())
    other_net.remove.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_orphans_skips_non_empty_networks(settings):
    docker_client = MagicMock()
    docker_client.containers.list = MagicMock(return_value=[])

    busy = MagicMock()
    busy.name = "td-sandbox-net-busy"
    busy.attrs = {"Containers": {"some-container": {}}}
    busy.reload = MagicMock()
    busy.remove = MagicMock()

    docker_client.networks.list = MagicMock(return_value=[busy])

    await cleanup_orphans_at_startup(
        settings=settings, docker_client=docker_client,
    )
    busy.remove.assert_not_called()
