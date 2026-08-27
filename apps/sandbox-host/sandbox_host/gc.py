"""Idle reclaim + startup orphan cleanup.

Two responsibilities:

1. **Periodic idle GC** — every gc_interval_seconds we scan the
   registry and tear down any sandbox whose last_request_at is
   older than idle_seconds. The proxy bumps last_request_at on
   every passed request, so an actively-used sandbox stays alive.

2. **Startup orphan cleanup** — when sandbox-host restarts, the
   in-memory registry is empty but docker may still have running
   sandbox containers from before the crash. We can't recover the
   pre-crash routing (host_port → task_id mapping is lost), so
   we tear them all down at startup. Anything restartable should
   be re-provisioned by core on demand.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import docker
from docker.errors import APIError

from .provisioning import _best_effort_cleanup, teardown
from .settings import SandboxHostSettings
from .state import SandboxRegistry

log = logging.getLogger(__name__)


async def reclaim_idle(
    *,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    docker_client: docker.DockerClient | None = None,
) -> int:
    """Tear down sandboxes that have been idle longer than the
    configured threshold. Returns the count reclaimed."""
    threshold = settings.idle_seconds
    now = datetime.now(UTC)

    stale: list[str] = []
    for record in await registry.list_all():
        idle_for = (now - record.last_request_at).total_seconds()
        if idle_for >= threshold:
            stale.append(record.task_id)

    for task_id in stale:
        try:
            await teardown(
                task_id=task_id,
                settings=settings,
                registry=registry,
                docker_client=docker_client,
            )
            log.info("reclaimed idle sandbox %s", task_id)
        except Exception as e:  # noqa: BLE001
            log.warning("idle reclaim of %s failed: %s", task_id, e)

    return len(stale)


async def gc_loop(
    *,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    stop_event: asyncio.Event,
) -> None:
    """Long-running task: poll for idle reclaim every interval. Exits
    cleanly when stop_event is set."""
    interval = settings.gc_interval_seconds
    log.info("gc loop starting (interval=%ds, idle threshold=%ds)",
             interval, settings.idle_seconds)
    while not stop_event.is_set():
        try:
            await reclaim_idle(settings=settings, registry=registry)
        except Exception as e:  # noqa: BLE001
            log.warning("gc iteration error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # event set during wait → exit
        except asyncio.TimeoutError:
            pass  # normal — continue loop
    log.info("gc loop exiting")


async def cleanup_orphans_at_startup(
    *,
    settings: SandboxHostSettings,
    docker_client: docker.DockerClient | None = None,
) -> int:
    """Find any docker containers/networks tagged as ccpt sandboxes
    from a previous incarnation and remove them. Called at lifespan
    startup before the GC loop starts.

    We use docker labels to identify them — provisioning sets
    `ccpt.sandbox.task_id=<id>` on every container.
    """
    client = docker_client or docker.from_env()

    removed = 0
    try:
        # All containers tagged as sandboxes (running OR stopped).
        containers = await asyncio.to_thread(
            client.containers.list,
            all=True,
            filters={"label": "ccpt.sandbox.task_id"},
        )
    except APIError as e:
        log.warning("startup orphan scan: docker API error: %s", e)
        return 0

    for c in containers:
        task_id = (c.labels or {}).get("ccpt.sandbox.task_id", "<unknown>")
        log.info("startup: cleaning up orphaned sandbox container %s (task=%s)",
                 c.name, task_id)
        await _best_effort_cleanup(
            client,
            c.name or "",
            f"{settings.network_prefix}-{task_id}",
            self_container_name=settings.self_container_name,
        )
        removed += 1

    # Also sweep any sandbox bridge networks that have no attached
    # containers. They linger as zombies otherwise.
    try:
        networks = await asyncio.to_thread(
            client.networks.list,
            filters={"name": settings.network_prefix},
        )
    except APIError as e:
        log.warning("startup orphan scan: networks list error: %s", e)
        return removed

    for n in networks:
        # Make sure it's actually one of ours (filter is substring-y).
        if not n.name.startswith(settings.network_prefix):
            continue
        try:
            await asyncio.to_thread(n.reload)
            attached = n.attrs.get("Containers") or {}
            # Filter out our own container — when sandbox-host restarts
            # after a sandbox container has died, the dead sandbox is
            # gone but our previous self.connect() leaves us attached
            # to that network. From docker's POV the network has 1
            # endpoint and won't remove; from our POV it's an orphan.
            # Disconnect ourselves first, then prune.
            self_name = settings.self_container_name
            other_endpoints = {
                cid: info for cid, info in attached.items()
                if (info or {}).get("Name") != self_name
            }
            if not other_endpoints:
                if attached and self_name:
                    try:
                        await asyncio.to_thread(
                            n.disconnect, self_name, force=True,
                        )
                        log.info(
                            "startup: detached self from orphan network %s",
                            n.name,
                        )
                    except APIError as e:
                        log.warning(
                            "startup: self-detach from %s failed: %s",
                            n.name, e,
                        )
                await asyncio.to_thread(n.remove)
                log.info("startup: removed orphan network %s", n.name)
        except APIError as e:
            log.warning("startup: failed to remove network %s: %s", n.name, e)

    return removed
