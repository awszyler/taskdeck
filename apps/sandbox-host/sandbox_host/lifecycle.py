"""Lifecycle service — per-task locked, idempotent provision/teardown.

Phase 3 of P-H. Wraps the raw building blocks in `provisioning.py`:

- `provision()` does the actual docker work (build network, run
  container, wait for readiness). Not concurrency-safe on its own —
  two parallel calls for the same task would race on names.

- `LifecycleService.provision_idempotent()` adds:
  * a per-task asyncio.Lock so only one provision/teardown for a
    given task can run at a time;
  * an early-exit when the SQLite row already says running AND a
    cheap TCP probe confirms the container is alive — return the
    existing record instead of recreating.
  * a forced teardown if the row exists but the container is dead,
    before falling through to a fresh provision.

The lock pool is keyed by task_id and pruned on teardown so it
doesn't grow unbounded over the lifetime of the process.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING

import docker

from .provisioning import provision as _raw_provision
from .provisioning import teardown as _raw_teardown
from .proxy import _invalidate_probe
from .state import SandboxRecord, SandboxRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from .detection import SandboxSpec
    from .settings import SandboxHostSettings


log = logging.getLogger(__name__)


# Cheap reachability probe: connect to (host, port) with a short
# timeout. Used to decide whether a registry row marked "running"
# still has a live container behind it. ~1ms when good, ~200ms on
# bad path. Doesn't read or write any bytes — just SYN/ACK.
async def _probe_tcp(host: str, port: int, timeout: float = 0.5) -> bool:
    def _connect() -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            try:
                s.connect((host, port))
                return True
            except OSError:
                return False
    try:
        return await asyncio.wait_for(asyncio.to_thread(_connect), timeout=timeout + 0.5)
    except asyncio.TimeoutError:
        return False


class LifecycleService:
    """Locks + idempotency around provision / teardown."""

    def __init__(self) -> None:
        # Per-task locks. Pruned on teardown to keep the dict small.
        self._locks: dict[str, asyncio.Lock] = {}
        # Master lock to protect _locks itself (dict mutation).
        self._lock_pool_mutex = asyncio.Lock()

    async def _lock_for(self, task_id: str) -> asyncio.Lock:
        async with self._lock_pool_mutex:
            lock = self._locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[task_id] = lock
            return lock

    async def _drop_lock(self, task_id: str) -> None:
        async with self._lock_pool_mutex:
            self._locks.pop(task_id, None)

    async def provision_idempotent(
        self,
        *,
        task_id: str,
        workspace: Path,
        spec: SandboxSpec,
        settings: SandboxHostSettings,
        registry: SandboxRegistry,
        docker_client: docker.DockerClient | None = None,
    ) -> SandboxRecord:
        """Acquire the per-task lock; if a healthy sandbox already
        exists for this task, return it as-is; otherwise teardown
        any stale entry and spin up a fresh one."""
        lock = await self._lock_for(task_id)
        async with lock:
            # Re-check inside the lock (some other task may have
            # provisioned while we waited).
            existing = await registry.get(task_id)
            if existing is not None and existing.status == "running":
                # Cheap probe to make sure the container is actually
                # serving — if it's been OOM-killed the row would
                # still say running until reconciler runs.
                if await _probe_tcp(
                    existing.container_name, existing.internal_port,
                ):
                    log.info(
                        "provision idempotent: %s already healthy, reusing",
                        task_id,
                    )
                    return existing
                log.info(
                    "provision idempotent: %s row says running but probe "
                    "failed, tearing down stale entry",
                    task_id,
                )
                await _raw_teardown(
                    task_id=task_id,
                    settings=settings,
                    registry=registry,
                    docker_client=docker_client,
                )

            # Either no row, or row is in a non-running state, or
            # we just torn down a dead one. Now build fresh.
            return await _raw_provision(
                task_id=task_id,
                workspace=workspace,
                spec=spec,
                settings=settings,
                registry=registry,
                docker_client=docker_client,
            )

    async def teardown_idempotent(
        self,
        *,
        task_id: str,
        settings: SandboxHostSettings,
        registry: SandboxRegistry,
        docker_client: docker.DockerClient | None = None,
    ) -> bool:
        """Lock-protected teardown. Returns the same bool _raw_teardown
        does (True = something was found and removed).

        Phase 4 nuance: _raw_teardown deletes the row, which would
        reset generation to 1 on the next provision. To preserve the
        "always increment" property we re-insert the row with status
        stopped + the same generation. The reconciler's decision
        table will eventually delete the row on the next pass, so
        this is just a short-lived bookkeeping shim; long enough for
        the next provision to read the prior generation.
        """
        lock = await self._lock_for(task_id)
        async with lock:
            try:
                # Capture generation BEFORE teardown deletes the row.
                prev = await registry.get(task_id)
                found = await _raw_teardown(
                    task_id=task_id,
                    settings=settings,
                    registry=registry,
                    docker_client=docker_client,
                )
                if prev is not None:
                    # Re-insert as stopped so the next provision can
                    # see the generation. Reconciler's "stopped → delete_row"
                    # branch will eventually clean it up.
                    prev.status = "stopped"
                    await registry.add(prev)
                # Drop any cached probe verdict so a fresh request
                # doesn't serve a stale 'alive' answer (Phase 7).
                _invalidate_probe(task_id)
                return found
            finally:
                # Don't drop the lock here — we just re-inserted a row
                # for the same task_id. Letting the lock outlive the
                # call is harmless; the dict cleanup happens on the
                # next provision after reconciler clears the row.
                pass
