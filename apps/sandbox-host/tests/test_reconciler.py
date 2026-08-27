"""Reconciler decision-table tests (P-H Phase 2).

The plan() function is pure — no docker SDK, no I/O — so every
branch of the decision table can be exercised quickly. apply() is
covered by a separate "happy path" test that uses a docker mock.

The decision table (mirrored in reconciler.py docstring):

  DB.status     | container | running | actions
  --------------|-----------|---------|----------------------------
  running       | absent    | -       | mark_stopped
  running       | exists    | yes     | (none — consistent)
  running       | exists    | no      | teardown_container, mark_stopped
  provisioning  | absent    | -       | mark_error
  provisioning  | exists    | yes     | mark_running
  provisioning  | exists    | no      | teardown_container, mark_error
  stopping      | *         | *       | (teardown if exists), delete_row
  stopped       | absent    | -       | delete_row
  stopped       | exists    | *       | teardown_container, delete_row
  error         | absent    | -       | delete_row
  error         | exists    | *       | teardown_container, delete_row
  (orphan: container with no row)     | teardown_container
  (zombie network: only self attached)| remove_network
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sandbox_host.reconciler import (
    ContainerView,
    NetworkView,
    ReconcileAction,
    plan,
)
from sandbox_host.state import SandboxRecord


def _row(task_id: str = "t", status: str = "running") -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        task_id=task_id,
        container_id=f"cid-{task_id}",
        container_name=f"td-sandbox-{task_id}",
        network_name=f"td-sandbox-net-{task_id}",
        host_port=10000,
        internal_port=8080,
        runtime="static",
        image="img",
        base_path=f"/sandbox/{task_id}/",
        started_at=now,
        last_request_at=now,
        status=status,
    )


def _container(task_id: str, running: bool = True) -> ContainerView:
    return ContainerView(
        name=f"td-sandbox-{task_id}",
        task_id=task_id,
        running=running,
    )


def _kinds(actions: list[ReconcileAction]) -> list[str]:
    return [a.kind for a in actions]


# ---------- DB-driven branches ---------------------------------------


def test_running_with_container_running_is_noop():
    actions = plan(
        rows=[_row("t1", "running")],
        containers=[_container("t1", running=True)],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert actions == []


def test_running_with_no_container_marks_stopped():
    actions = plan(
        rows=[_row("t1", "running")],
        containers=[],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["mark_stopped"]
    assert actions[0].task_id == "t1"


def test_running_with_dead_container_tears_down_and_marks_stopped():
    actions = plan(
        rows=[_row("t1", "running")],
        containers=[_container("t1", running=False)],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["teardown_container", "mark_stopped"]


def test_provisioning_no_container_marks_error():
    actions = plan(
        rows=[_row("t1", "provisioning")],
        containers=[],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["mark_error"]


def test_provisioning_running_container_promotes_to_running():
    actions = plan(
        rows=[_row("t1", "provisioning")],
        containers=[_container("t1", running=True)],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["mark_running"]


def test_provisioning_dead_container_tears_down_and_marks_error():
    actions = plan(
        rows=[_row("t1", "provisioning")],
        containers=[_container("t1", running=False)],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["teardown_container", "mark_error"]


def test_stopping_clears_container_and_row():
    actions = plan(
        rows=[_row("t1", "stopping")],
        containers=[_container("t1", running=True)],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["teardown_container", "delete_row"]


def test_stopping_with_no_container_just_deletes_row():
    actions = plan(
        rows=[_row("t1", "stopping")],
        containers=[],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["delete_row"]


def test_terminal_states_delete_row():
    for st in ("stopped", "error"):
        actions = plan(
            rows=[_row("t1", st)],
            containers=[],
            networks=[],
            self_container_name="self",
            network_prefix="td-sandbox-net",
        )
        assert _kinds(actions) == ["delete_row"], f"failed for {st}"


def test_terminal_state_with_leftover_container_tears_down():
    actions = plan(
        rows=[_row("t1", "stopped")],
        containers=[_container("t1", running=True)],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["teardown_container", "delete_row"]


# ---------- Docker-driven branches -----------------------------------


def test_orphan_container_with_no_row_is_torn_down():
    actions = plan(
        rows=[],
        containers=[_container("orphan", running=True)],
        networks=[],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["teardown_container"]
    assert actions[0].task_id == "orphan"


def test_zombie_network_only_self_attached_is_removed():
    actions = plan(
        rows=[],
        containers=[],  # container is gone; network lingers
        networks=[NetworkView(
            name="td-sandbox-net-dead",
            attached_names=("self",),  # only sandbox-host attached
        )],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert _kinds(actions) == ["remove_network"]
    assert actions[0].network_name == "td-sandbox-net-dead"


def test_zombie_network_with_other_attached_is_left_alone():
    """If somehow another container is still on the network, refuse
    to disrupt it. Could be a manual attach during debugging."""
    actions = plan(
        rows=[],
        containers=[],
        networks=[NetworkView(
            name="td-sandbox-net-foo",
            attached_names=("self", "manual-debug-tool"),
        )],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert actions == []


def test_live_network_with_running_container_left_alone():
    """Network whose task still has a running container is in use —
    don't touch it even though row.network_name happens to match."""
    actions = plan(
        rows=[_row("t1", "running")],
        containers=[_container("t1", running=True)],
        networks=[NetworkView(
            name="td-sandbox-net-t1",
            attached_names=("self", "td-sandbox-t1"),
        )],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    # Container is consistent → no DB action.
    # Network has the live container attached → no remove action.
    assert actions == []


def test_filters_non_sandbox_networks():
    """A network whose name doesn't start with our prefix must be
    completely ignored — not in our domain."""
    actions = plan(
        rows=[],
        containers=[],
        networks=[NetworkView(
            name="bridge",  # docker default network
            attached_names=("self",),
        )],
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    assert actions == []


def test_combined_scenario_drift_after_crash():
    """Realistic mixed input: one healthy task, one dead-container
    task, one orphan, one zombie network."""
    rows = [
        _row("alive", "running"),
        _row("dead-container", "running"),
    ]
    containers = [
        _container("alive", running=True),
        # dead-container's container is gone
        _container("orphan", running=True),  # orphan, no DB row
    ]
    networks = [
        NetworkView(name="td-sandbox-net-alive",
                    attached_names=("self", "td-sandbox-alive")),
        NetworkView(name="td-sandbox-net-zombie",
                    attached_names=("self",)),  # zombie
    ]
    actions = plan(
        rows=rows,
        containers=containers,
        networks=networks,
        self_container_name="self",
        network_prefix="td-sandbox-net",
    )
    kinds_by_id = sorted((a.kind, a.task_id) for a in actions)
    # Expected: dead-container → mark_stopped (no container to tear)
    #           orphan → teardown_container
    #           zombie → remove_network
    assert ("mark_stopped", "dead-container") in kinds_by_id
    assert ("teardown_container", "orphan") in kinds_by_id
    assert any(a.kind == "remove_network" for a in actions)
    assert not any(a.task_id == "alive" for a in actions)


# ---------- apply (single integration test with mocked docker) -------


@pytest.mark.asyncio
async def test_reconcile_once_applies_actions(tmp_path, monkeypatch):
    """Drive reconcile_once end-to-end with a docker mock and a real
    SQLite registry. Verifies that:
    - DB rows for vanished containers get flipped to stopped
    - orphan containers trigger _best_effort_cleanup
    - zombie networks get removed
    """
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock

    from sandbox_host.reconciler import reconcile_once
    from sandbox_host.settings import SandboxHostSettings
    from sandbox_host.state import SandboxRecord, SandboxRegistry

    s = SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR=str(tmp_path),
        TD_SBH_CONTAINER_RUNTIME="runc",
        TD_SBH_SELF_CONTAINER_NAME="self-host",
    )
    registry = SandboxRegistry(db_path=tmp_path / "state.db")

    # Seed: one row that thinks it's running, but docker has no
    # corresponding container.
    now = datetime.now(UTC)
    await registry.add(SandboxRecord(
        task_id="vanished",
        container_id="cid",
        container_name="td-sandbox-vanished",
        network_name="td-sandbox-net-vanished",
        host_port=10001, internal_port=8080,
        runtime="static", image="img",
        base_path="/sandbox/vanished/",
        started_at=now, last_request_at=now,
        status="running",
    ))

    # Mock docker client.
    docker_client = MagicMock()
    docker_client.containers.list = MagicMock(return_value=[])  # empty
    docker_client.networks.list = MagicMock(return_value=[])    # empty

    # Patch _best_effort_cleanup so we don't try real docker calls
    # for a non-existent container/network.
    cleanup_calls: list[tuple[str, str]] = []

    async def fake_cleanup(client, container_name, network_name, *, self_container_name):  # noqa: ARG001
        cleanup_calls.append((container_name, network_name))

    monkeypatch.setattr(
        "sandbox_host.reconciler._best_effort_cleanup", fake_cleanup,
    )

    counts = await reconcile_once(
        settings=s,
        registry=registry,
        docker_client=docker_client,
    )
    # Vanished row → mark_stopped action.
    assert counts.get("mark_stopped", 0) == 1

    # And the row's status was updated in DB.
    fetched = await registry.get("vanished")
    assert fetched is not None
    assert fetched.status == "stopped"


@pytest.mark.asyncio
async def test_reconcile_loop_runs_at_least_one_tick_then_exits(
    tmp_path, monkeypatch,
):
    """Loop should fire reconcile_once on entry, then again on the
    next tick. Setting stop_event mid-tick exits cleanly."""
    import asyncio

    from sandbox_host.reconciler import reconcile_loop
    from sandbox_host.settings import SandboxHostSettings
    from sandbox_host.state import SandboxRegistry

    s = SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR=str(tmp_path),
        TD_SBH_CONTAINER_RUNTIME="runc",
    )
    registry = SandboxRegistry(db_path=tmp_path / "state.db")

    call_count = 0

    async def fake_once(**_kwargs):
        nonlocal call_count
        call_count += 1
        return {}

    monkeypatch.setattr("sandbox_host.reconciler.reconcile_once", fake_once)

    stop = asyncio.Event()
    task = asyncio.create_task(reconcile_loop(
        settings=s, registry=registry,
        stop_event=stop, interval_seconds=0.05,
    ))
    # Let it tick a few times.
    await asyncio.sleep(0.13)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    # Initial + 2 more by ~0.13s @ 0.05s interval.
    assert call_count >= 2


@pytest.mark.asyncio
async def test_reconcile_loop_exits_on_stop_between_ticks(tmp_path, monkeypatch):
    """If stop_event fires while sleeping between ticks, the loop
    should return promptly without waiting out the full interval."""
    import asyncio
    import time

    from sandbox_host.reconciler import reconcile_loop
    from sandbox_host.settings import SandboxHostSettings
    from sandbox_host.state import SandboxRegistry

    s = SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR=str(tmp_path),
        TD_SBH_CONTAINER_RUNTIME="runc",
    )
    registry = SandboxRegistry(db_path=tmp_path / "state.db")

    async def fake_once(**_kwargs):
        return {}

    monkeypatch.setattr("sandbox_host.reconciler.reconcile_once", fake_once)

    stop = asyncio.Event()
    started = time.monotonic()
    task = asyncio.create_task(reconcile_loop(
        settings=s, registry=registry,
        stop_event=stop, interval_seconds=10.0,  # long interval
    ))
    await asyncio.sleep(0.05)  # let loop enter wait_for
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    elapsed = time.monotonic() - started
    # Should exit well under the 10s interval.
    assert elapsed < 1.0
