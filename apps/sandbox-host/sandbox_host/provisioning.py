"""Docker provisioning for sandboxes.

Single entrypoint: `provision()` takes a task_id + workspace + SandboxSpec,
spins up a gVisor-isolated docker container, waits for the user app
to bind its port, and registers the resulting SandboxRecord.

Reverse direction: `teardown()` cleanly stops + removes the container
and its dedicated bridge network.

Design notes:

- **Per-sandbox bridge network** (`td-sandbox-net-<task_id>`) so
  sandboxes can't talk to each other or to the taskdeck_default
  network where postgres/core live. host bind-mount + bridge gives
  us a clean blast-radius boundary; gVisor adds the kernel layer.

- **Random host port** assigned by docker (`-p 0:internal_port`) and
  read back from container.attrs. This avoids port-allocation logic
  and races.

- **Readiness check** is HTTP — we poll http://host:host_port/ until
  it returns *anything* (any status code), or timeout. Some user
  apps don't return 200 at root but do bind the port (e.g. APIs
  with no GET /); a TCP connect would suffice but HTTP is more
  common in practice and tells us the listener is actually serving.

- **Rollback on failure** is best-effort. If the container partly
  starts but readiness fails, we stop+remove it and the network.
  Failures during cleanup are logged, not raised.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from datetime import UTC, datetime
from pathlib import Path

import docker
import httpx
from docker.errors import APIError, NotFound

from .detection import SandboxSpec
from .settings import SandboxHostSettings
from .state import SandboxRecord, SandboxRegistry

log = logging.getLogger(__name__)


class ProvisionError(Exception):
    """Raised when a sandbox can't be started. Caller catches and
    surfaces a 5xx to the API client."""


class CapacityError(Exception):
    """Raised when max_concurrent is hit. Caller surfaces 429."""


def _container_name(task_id: str, generation: int = 1) -> str:
    """Phase 4: include the generation counter so retries / reruns
    can never collide with a leftover from the previous attempt.

    generation defaults to 1 for callers that don't track it (e.g. the
    legacy teardown-by-name path; the worst-case there is "miss the
    container we wanted to clean", and the reconciler will sweep
    later)."""
    return f"td-sandbox-{task_id}-g{generation}"


def _network_name(prefix: str, task_id: str, generation: int = 1) -> str:
    return f"{prefix}-{task_id}-g{generation}"


def _bridge_iface_name(task_id: str) -> str:
    # Linux IFNAMSIZ caps interface names at 15 chars. The host-level
    # iptables rules in deploy/sandbox-iptables.sh match on `-i td-sbx-+`
    # to identify *sandbox* bridges (vs core/postgres/caddy on br-*),
    # so the prefix must stay stable. 9 chars prefix + 6 chars task-id
    # slice = 15. Collisions across short slices are tolerable: docker
    # bridge create errors on dup names → caller cleans up first.
    return f"td-sbx-{task_id[:6]}"


def _image_for(spec: SandboxSpec, settings: SandboxHostSettings) -> str:
    """Map detection's image_key to the configured image tag."""
    return {
        "static": settings.image_static,
        "node": settings.image_node,
        "python": settings.image_python,
    }[spec.image_key]


async def _wait_ready(
    host: str,
    port: int,
    timeout_seconds: int,
) -> None:
    """Poll `http://<host>:<port>/` until something serves HTTP, or
    timeout. Accepts any HTTP response (4xx/5xx fine) as "ready" —
    a 404 just means the user app's root isn't mapped, but the
    listener is up.

    `host` is typically the sandbox container name when sandbox-host
    runs in docker-compose (it joins the per-sandbox network and
    talks to the container directly). For dev with sandbox-host
    running on the host loopback, it can be 127.0.0.1.
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    interval = 0.3
    last_err: Exception | None = None

    async with httpx.AsyncClient(timeout=2.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                await client.get(f"http://{host}:{port}/")
                return  # any response is fine
            except (
                httpx.ConnectError,
                httpx.ReadError,         # socket closed mid-handshake
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.ConnectTimeout,
            ) as e:
                last_err = e
            await asyncio.sleep(interval)
            # Mild backoff so a slow-starting node app doesn't get
            # hammered with connect attempts.
            interval = min(interval * 1.2, 1.0)

    raise ProvisionError(
        f"sandbox port {host}:{port} did not become ready in "
        f"{timeout_seconds}s"
        + (f" (last error: {last_err})" if last_err else "")
    )


def _read_host_port(container, internal_port: int) -> int:
    """Pull the host-side port docker assigned to internal_port.

    Container attrs structure:
      NetworkSettings.Ports = {
        "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32811"}]
      }
    """
    container.reload()  # populate Ports after start
    ports = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
    key = f"{internal_port}/tcp"
    bindings = ports.get(key)
    if not bindings:
        raise ProvisionError(
            f"docker did not publish port {internal_port}/tcp on the container"
        )
    host_port = bindings[0].get("HostPort")
    if not host_port:
        raise ProvisionError(f"empty HostPort in binding {bindings[0]!r}")
    return int(host_port)


async def provision(
    *,
    task_id: str,
    workspace: Path,
    spec: SandboxSpec,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    docker_client: docker.DockerClient | None = None,
) -> SandboxRecord:
    """Spin up a sandbox container for the given task. Returns the
    SandboxRecord after the container is ready to serve traffic.

    Raises:
        CapacityError: max_concurrent would be exceeded.
        ProvisionError: anything else (image missing, container died,
                        readiness timeout, etc.). Container/network
                        are cleaned up before raising.
    """
    # 1. Capacity check.
    if len(registry) >= settings.max_concurrent:
        raise CapacityError(
            f"max_concurrent={settings.max_concurrent} sandboxes already running"
        )

    # 2. Client (allow injection for tests).
    client = docker_client or docker.from_env()

    # P-H Phase 4: bump the generation. If a row from a previous run
    # exists (any status), we're a fresh attempt — N+1. Otherwise
    # start at 1. This means resource names *never* collide across
    # provision attempts, even if cleanup of the previous gen lagged.
    prev = await registry.get(task_id)
    generation = (prev.generation + 1) if prev is not None else 1

    container_name = _container_name(task_id, generation)
    network_name = _network_name(settings.network_prefix, task_id, generation)
    image = _image_for(spec, settings)
    base_path = f"/sandbox/{task_id}/"

    # If a stale container/network from a previous run exists, clean
    # before creating new ones. Caller might be retrying after a
    # provision-time crash.
    await _best_effort_cleanup(
        client, container_name, network_name,
        self_container_name=settings.self_container_name,
    )

    # 3. Create dedicated bridge network. This is the structural
    #    isolation that keeps sandboxes from reaching postgres/core.
    #
    #    The fixed `td-sbx-*` bridge name is what the host iptables
    #    rules match on (-i td-sbx-+) to drop sandbox egress to the
    #    EC2 metadata IP and the docker default subnet. Without this
    #    opt, docker assigns a random `br-<id>` name and the rules
    #    can't find the interface.
    bridge_iface = _bridge_iface_name(task_id)
    try:
        await asyncio.to_thread(
            client.networks.create,
            name=network_name,
            driver="bridge",
            internal=False,  # outbound internet allowed by default
            check_duplicate=False,
            options={"com.docker.network.bridge.name": bridge_iface},
        )
    except APIError as e:
        raise ProvisionError(f"failed to create network {network_name}: {e}") from e

    container = None
    try:
        # 4. Run container. We do NOT publish ports to the host —
        #    sandbox-host (running in compose) joins the sandbox
        #    network below and reaches the container directly by
        #    DNS name. This avoids host port-forward conflicts and
        #    keeps the sandbox unreachable from the public host.
        try:
            container = await asyncio.to_thread(
                client.containers.run,
                image,
                detach=True,
                name=container_name,
                runtime=settings.container_runtime,
                network=network_name,
                mem_limit=f"{settings.memory_limit_mb}m",
                cpu_quota=int(settings.cpu_limit * 100_000),
                cpu_period=100_000,
                pids_limit=settings.pids_limit,
                tmpfs={"/tmp": f"size={settings.tmpfs_size_mb}m"},
                volumes={
                    str(workspace): {"bind": "/workspace", "mode": "rw"},
                },
                # NOTE: no `ports=` — intentional, see comment above.
                environment={
                    "TD_INSTALL_CMD": spec.install_cmd or "",
                    "TD_START_CMD": spec.start_cmd,
                    "TD_BASE_PATH": base_path,
                },
                labels={
                    "ccpt.sandbox.task_id": task_id,
                    "ccpt.sandbox.runtime": spec.runtime,
                    "ccpt.sandbox.source": spec.source,
                },
                # Don't auto-restart — if it crashes we want the user
                # to see "failed" not "perpetually-restarting".
                restart_policy={"Name": "no"},
            )
        except APIError as e:
            raise ProvisionError(f"docker run failed: {e}") from e

        # 5. Join sandbox-host's container into the sandbox network so
        #    we can reach the new container by name. Skipped in dev/
        #    test (when sandbox-host runs as a host process or the
        #    self-container name is unset).
        if settings.self_container_name:
            try:
                sandbox_net = await asyncio.to_thread(
                    client.networks.get, network_name,
                )
                await asyncio.to_thread(
                    sandbox_net.connect, settings.self_container_name,
                )
            except (APIError, NotFound) as e:
                # Already connected (idempotent re-provision) is fine,
                # as is "network not found" in mock environments where
                # networks.get returns NotFound.
                if not (
                    "already exists" in str(e)
                    or isinstance(e, NotFound)
                ):
                    raise ProvisionError(
                        f"failed to attach sandbox-host to {network_name}: {e}",
                    ) from e

        # internal_port is what the user app bound *inside* the container.
        # We talk to the container directly on the docker network.
        host_port = spec.port  # for the upstream URL the proxy uses
        upstream_host = container_name

        # 6. Wait for the user app to bind its port.
        try:
            await _wait_ready(
                upstream_host, host_port, settings.startup_timeout_seconds,
            )
        except ProvisionError:
            # Capture container logs for the error path so the API
            # caller can surface "your app crashed because <stderr>".
            try:
                logs = container.logs(tail=50, stdout=True, stderr=True).decode(
                    errors="replace",
                )
                log.warning(
                    "sandbox %s readiness timed out; container logs:\n%s",
                    task_id, logs,
                )
            except APIError:
                pass
            raise

        # 7. Build record + register.
        now = datetime.now(UTC)
        record = SandboxRecord(
            task_id=task_id,
            container_id=container.id or "",
            container_name=container_name,
            network_name=network_name,
            host_port=host_port,
            internal_port=spec.port,
            runtime=spec.runtime,
            image=image,
            base_path=base_path,
            started_at=now,
            last_request_at=now,
            status="running",
            generation=generation,
        )
        await registry.add(record)
        log.info(
            "sandbox %s ready: image=%s host_port=%d (runtime=%s)",
            task_id, image, host_port, spec.runtime,
        )
        return record

    except Exception:
        # Rollback on any failure after network create.
        await _best_effort_cleanup(
            client, container_name, network_name,
            self_container_name=settings.self_container_name,
        )
        raise


async def teardown(
    *,
    task_id: str,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    docker_client: docker.DockerClient | None = None,
) -> bool:
    """Stop + remove the sandbox container and its network.

    Returns True if a sandbox was found and removed, False if there
    was nothing to clean up. Idempotent — safe to call repeatedly."""
    record = await registry.remove(task_id)
    if record is None:
        # Try the by-name path anyway in case state and docker drifted.
        client = docker_client or docker.from_env()
        await _best_effort_cleanup(
            client,
            _container_name(task_id),
            _network_name(settings.network_prefix, task_id),
            self_container_name=settings.self_container_name,
        )
        return False

    client = docker_client or docker.from_env()
    await _best_effort_cleanup(
        client, record.container_name, record.network_name,
        self_container_name=settings.self_container_name,
    )
    log.info("sandbox %s torn down", task_id)
    return True


async def _best_effort_cleanup(
    client: docker.DockerClient,
    container_name: str,
    network_name: str,
    *,
    self_container_name: str = "",
) -> None:
    """Stop+remove a container and remove a network; log but never
    raise. Used both as failure-rollback and as teardown's worker.

    self_container_name (when non-empty) is the sandbox-host container's
    own name. We may have attached ourselves to this network at provision
    time for the reverse proxy; if the sandbox container died but our
    attach lingers, networks.remove() fails with "active endpoints" even
    though the only endpoint is us. Disconnect self first to make the
    network removable. Same root cause as the startup orphan sweep in
    gc.cleanup_orphans_at_startup; both paths must do this dance.
    """
    try:
        c = await asyncio.to_thread(client.containers.get, container_name)
    except NotFound:
        c = None
    except APIError as e:
        log.warning("container.get(%s) failed: %s", container_name, e)
        c = None

    if c is not None:
        try:
            await asyncio.to_thread(c.stop, timeout=5)
        except APIError as e:
            log.warning("container.stop(%s) failed: %s", container_name, e)
        try:
            await asyncio.to_thread(c.remove, force=True)
        except APIError as e:
            log.warning("container.remove(%s) failed: %s", container_name, e)

    try:
        n = await asyncio.to_thread(client.networks.get, network_name)
    except NotFound:
        return
    except APIError as e:
        log.warning("networks.get(%s) failed: %s", network_name, e)
        return

    # Detach self first if we're attached. force=True is correct here:
    # the sandbox is being torn down, we don't need a clean handshake.
    if self_container_name:
        try:
            await asyncio.to_thread(n.reload)
            attached = n.attrs.get("Containers") or {}
            self_attached = any(
                (info or {}).get("Name") == self_container_name
                for info in attached.values()
            )
            if self_attached:
                try:
                    await asyncio.to_thread(
                        n.disconnect, self_container_name, force=True,
                    )
                except APIError as e:
                    log.warning(
                        "networks.disconnect(%s, %s) failed: %s",
                        network_name, self_container_name, e,
                    )
        except APIError as e:
            log.warning("networks.reload(%s) failed: %s", network_name, e)

    try:
        await asyncio.to_thread(n.remove)
    except APIError as e:
        log.warning("networks.remove(%s) failed: %s", network_name, e)


def free_host_port_hint() -> int:
    """Return a likely-free port. Not used by docker (which assigns
    its own random port) but useful for local dev when bypassing
    docker. Bind a socket on :0, read the port, close — there's a
    race window but this is for dev convenience only."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()
