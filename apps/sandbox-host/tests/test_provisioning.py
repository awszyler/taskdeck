"""Unit tests for sandbox_host.provisioning.

These mock the docker SDK and httpx — no actual containers run.
End-to-end tests live in test_provisioning_e2e.py and require docker
+ gVisor on the host (skipped when unavailable).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from docker.errors import APIError, NotFound
from sandbox_host.detection import SandboxSpec
from sandbox_host.provisioning import (
    CapacityError,
    ProvisionError,
    _best_effort_cleanup,
    _container_name,
    _image_for,
    _network_name,
    _read_host_port,
    provision,
    teardown,
)
from sandbox_host.settings import SandboxHostSettings
from sandbox_host.state import SandboxRegistry


@pytest.fixture
def settings():
    return SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR="/tmp/td-test",
        TD_SBH_CONTAINER_RUNTIME="runc",
        TD_SBH_MAX_CONCURRENT=2,
        TD_SBH_STARTUP_TIMEOUT=2,
        # Skip the "attach sandbox-host to sandbox network" step in
        # tests — we don't have a real container to attach.
        TD_SBH_SELF_CONTAINER_NAME="",
    )


@pytest.fixture
def registry(tmp_path):
    return SandboxRegistry(db_path=tmp_path / "state.db")


@pytest.fixture
def static_spec():
    return SandboxSpec(
        runtime="static",
        image_key="static",
        install_cmd=None,
        start_cmd="nginx -g 'daemon off;'",
        port=8080,
        source="auto:static",
    )


def _mock_container(host_port: int = 32811):
    """Build a docker container mock with a populated Ports binding."""
    container = MagicMock()
    container.id = "deadbeef" * 8
    container.attrs = {
        "NetworkSettings": {
            "Ports": {
                "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(host_port)}],
            },
        },
    }
    container.reload = MagicMock()
    container.logs = MagicMock(return_value=b"app started\n")
    container.stop = MagicMock()
    container.remove = MagicMock()
    return container


# ---------- helpers (pure) -------------------------------------------


def test_container_name_format():
    # P-H Phase 4: names embed the generation counter.
    assert _container_name("abc-123") == "td-sandbox-abc-123-g1"
    assert _container_name("abc-123", 7) == "td-sandbox-abc-123-g7"


def test_network_name_format():
    assert _network_name("td-sandbox-net", "xyz") == "td-sandbox-net-xyz-g1"
    assert _network_name("td-sandbox-net", "xyz", 3) == "td-sandbox-net-xyz-g3"


@pytest.mark.asyncio
async def test_provision_increments_generation_after_prior_run(
    settings, registry, static_spec, tmp_path, monkeypatch,
):
    """A second provision for the same task_id (after a teardown leaves
    a row behind) must use generation N+1 so the docker resource names
    don't collide with leftover state."""
    from datetime import UTC, datetime

    from sandbox_host.state import SandboxRecord

    # Seed: a prior run finished, status=stopped, generation=4.
    now = datetime.now(UTC)
    await registry.add(SandboxRecord(
        task_id="t-bump",
        container_id="prev",
        container_name="td-sandbox-t-bump-g4",
        network_name="td-sandbox-net-t-bump-g4",
        host_port=8080, internal_port=8080,
        runtime="static", image="img",
        base_path="/sandbox/t-bump/",
        started_at=now, last_request_at=now,
        status="stopped",
        generation=4,
    ))

    container = _mock_container(host_port=8080)
    docker_client = MagicMock()
    docker_client.containers.run = MagicMock(return_value=container)
    docker_client.networks.create = MagicMock()
    docker_client.containers.get = MagicMock(side_effect=NotFound("none"))
    docker_client.networks.get = MagicMock(side_effect=NotFound("none"))

    async def fake_wait_ready(host, port, timeout_seconds):  # noqa: ARG001
        return None
    monkeypatch.setattr(
        "sandbox_host.provisioning._wait_ready", fake_wait_ready,
    )

    record = await provision(
        task_id="t-bump",
        workspace=tmp_path,
        spec=static_spec,
        settings=settings,
        registry=registry,
        docker_client=docker_client,
    )

    assert record.generation == 5
    assert record.container_name == "td-sandbox-t-bump-g5"
    assert record.network_name == "td-sandbox-net-t-bump-g5"


def test_image_for_static(settings, static_spec):
    assert _image_for(static_spec, settings) == settings.image_static


def test_image_for_node(settings):
    spec = SandboxSpec(
        runtime="node", image_key="node", install_cmd="npm i",
        start_cmd="npm run dev", port=3000, source="auto:node",
    )
    assert _image_for(spec, settings) == settings.image_node


def test_read_host_port_happy_path():
    container = _mock_container(host_port=12345)
    assert _read_host_port(container, 8080) == 12345


def test_read_host_port_missing_binding_raises():
    container = MagicMock()
    container.attrs = {"NetworkSettings": {"Ports": {}}}
    container.reload = MagicMock()
    with pytest.raises(ProvisionError, match="did not publish"):
        _read_host_port(container, 8080)


# ---------- capacity --------------------------------------------------


@pytest.mark.asyncio
async def test_provision_rejects_when_at_capacity(
    settings, registry, static_spec, tmp_path,
):
    # Pre-fill the registry to capacity.
    from datetime import UTC, datetime

    from sandbox_host.state import SandboxRecord
    now = datetime.now(UTC)
    for i in range(settings.max_concurrent):
        await registry.add(SandboxRecord(
            task_id=f"existing-{i}",
            container_id="x", container_name="x", network_name="x",
            host_port=10000 + i, internal_port=8080, runtime="static",
            image="x", base_path="/", started_at=now, last_request_at=now,
        ))

    with pytest.raises(CapacityError, match="max_concurrent"):
        await provision(
            task_id="new",
            workspace=tmp_path,
            spec=static_spec,
            settings=settings,
            registry=registry,
        )


# ---------- happy path with mocked docker -----------------------------


@pytest.mark.asyncio
async def test_provision_happy_path(
    settings, registry, static_spec, tmp_path, monkeypatch,
):
    container = _mock_container(host_port=32811)
    docker_client = MagicMock()
    docker_client.containers.run = MagicMock(return_value=container)
    docker_client.networks.create = MagicMock()
    docker_client.containers.get = MagicMock(side_effect=NotFound("no stale"))
    docker_client.networks.get = MagicMock(side_effect=NotFound("no stale"))

    # Patch httpx readiness check to return immediately.
    async def fake_wait_ready(host, port, timeout_seconds):  # noqa: ARG001
        return None
    monkeypatch.setattr(
        "sandbox_host.provisioning._wait_ready", fake_wait_ready,
    )

    record = await provision(
        task_id="t-happy",
        workspace=tmp_path,
        spec=static_spec,
        settings=settings,
        registry=registry,
        docker_client=docker_client,
    )

    assert record.task_id == "t-happy"
    # host_port and internal_port are both equal to spec.port now —
    # we don't publish ports to the host (sandbox-host talks to the
    # container by docker DNS name on the per-sandbox network).
    assert record.host_port == 8080
    assert record.internal_port == 8080
    assert record.runtime == "static"
    assert record.image == settings.image_static
    assert record.base_path == "/sandbox/t-happy/"
    assert record.status == "running"

    # Registered. SQLite-backed registry returns a fresh instance per
    # call (no identity), so compare by primary key + key fields.
    fetched = await registry.get("t-happy")
    assert fetched is not None
    assert fetched.task_id == record.task_id
    assert fetched.container_id == record.container_id
    assert fetched.host_port == record.host_port

    # Network was created with the right name (incl Phase 4 -g1).
    docker_client.networks.create.assert_called_once()
    call_kwargs = docker_client.networks.create.call_args.kwargs
    assert call_kwargs["name"] == "td-sandbox-net-t-happy-g1"
    assert call_kwargs["driver"] == "bridge"
    # Bridge interface name is fixed-prefix `td-sbx-<task_id[:6]>` so
    # host-level iptables rules can match `-i td-sbx-+` to identify
    # sandbox bridges. Generation is NOT in the bridge name (no need —
    # docker recreates the network each provision).
    assert call_kwargs["options"] == {
        "com.docker.network.bridge.name": "td-sbx-t-happ",
    }

    # Container.run called with runtime=runc (per fixture override).
    docker_client.containers.run.assert_called_once()
    run_kwargs = docker_client.containers.run.call_args.kwargs
    assert run_kwargs["runtime"] == "runc"
    assert run_kwargs["name"] == "td-sandbox-t-happy-g1"
    assert run_kwargs["network"] == "td-sandbox-net-t-happy-g1"
    assert run_kwargs["mem_limit"] == "1024m"
    # CPU is converted to docker quota/period.
    assert run_kwargs["cpu_period"] == 100_000
    assert run_kwargs["cpu_quota"] == 100_000  # 1.0 * 100k
    # Env carries the entrypoint contract.
    env = run_kwargs["environment"]
    assert env["TD_START_CMD"] == "nginx -g 'daemon off;'"
    assert env["TD_BASE_PATH"] == "/sandbox/t-happy/"


@pytest.mark.asyncio
async def test_provision_rolls_back_on_readiness_timeout(
    settings, registry, static_spec, tmp_path, monkeypatch,
):
    container = _mock_container()
    docker_client = MagicMock()
    docker_client.networks.create = MagicMock()
    docker_client.containers.run = MagicMock(return_value=container)
    docker_client.containers.get = MagicMock(return_value=container)
    network = MagicMock()
    docker_client.networks.get = MagicMock(return_value=network)

    async def fake_wait_ready(host, port, timeout_seconds):  # noqa: ARG001
        raise ProvisionError("timed out")
    monkeypatch.setattr(
        "sandbox_host.provisioning._wait_ready", fake_wait_ready,
    )

    with pytest.raises(ProvisionError, match="timed out"):
        await provision(
            task_id="t-rollback",
            workspace=tmp_path,
            spec=static_spec,
            settings=settings,
            registry=registry,
            docker_client=docker_client,
        )

    # Registry stayed clean.
    assert await registry.get("t-rollback") is None
    # Container was stopped + removed (best-effort cleanup ran).
    # Note: cleanup runs twice — once at start (sweep stale) and
    # once on rollback. Both find the same mocked container/network.
    container.stop.assert_called()
    container.remove.assert_called()
    network.remove.assert_called()


@pytest.mark.asyncio
async def test_provision_rolls_back_on_docker_run_failure(
    settings, registry, static_spec, tmp_path,
):
    docker_client = MagicMock()
    docker_client.networks.create = MagicMock()
    docker_client.containers.run = MagicMock(
        side_effect=APIError("image not found"),
    )
    docker_client.containers.get = MagicMock(side_effect=NotFound("none"))
    network = MagicMock()
    docker_client.networks.get = MagicMock(return_value=network)

    with pytest.raises(ProvisionError, match="docker run failed"):
        await provision(
            task_id="t-image-missing",
            workspace=tmp_path,
            spec=static_spec,
            settings=settings,
            registry=registry,
            docker_client=docker_client,
        )

    assert await registry.get("t-image-missing") is None
    # Cleanup ran on rollback (network.get found the just-created network).
    network.remove.assert_called()


# ---------- teardown --------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_removes_existing_sandbox(settings, registry):
    from datetime import UTC, datetime

    from sandbox_host.state import SandboxRecord
    now = datetime.now(UTC)
    await registry.add(SandboxRecord(
        task_id="t-down",
        container_id="cid", container_name="td-sandbox-t-down",
        network_name="td-sandbox-net-t-down",
        host_port=11111, internal_port=8080, runtime="static",
        image="x", base_path="/", started_at=now, last_request_at=now,
    ))

    container = MagicMock()
    network = MagicMock()
    docker_client = MagicMock()
    docker_client.containers.get = MagicMock(return_value=container)
    docker_client.networks.get = MagicMock(return_value=network)

    found = await teardown(
        task_id="t-down",
        settings=settings,
        registry=registry,
        docker_client=docker_client,
    )
    assert found is True
    assert await registry.get("t-down") is None
    container.stop.assert_called()
    container.remove.assert_called()
    network.remove.assert_called_once()


@pytest.mark.asyncio
async def test_teardown_returns_false_for_unknown_task(settings, registry):
    docker_client = MagicMock()
    docker_client.containers.get = MagicMock(side_effect=NotFound("none"))
    docker_client.networks.get = MagicMock(side_effect=NotFound("none"))
    found = await teardown(
        task_id="never-existed",
        settings=settings,
        registry=registry,
        docker_client=docker_client,
    )
    assert found is False


@pytest.mark.asyncio
async def test_best_effort_cleanup_swallows_errors():
    docker_client = MagicMock()
    container = MagicMock()
    container.stop = MagicMock(side_effect=APIError("boom"))
    container.remove = MagicMock(side_effect=APIError("boom"))
    docker_client.containers.get = MagicMock(return_value=container)
    network = MagicMock()
    network.remove = MagicMock(side_effect=APIError("boom"))
    docker_client.networks.get = MagicMock(return_value=network)

    # Must not raise — these are best-effort.
    await _best_effort_cleanup(docker_client, "x", "y")
