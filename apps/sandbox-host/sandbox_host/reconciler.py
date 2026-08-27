"""Sandbox state reconciler (P-H Phase 2).

Diff what we *intended* (rows in the SQLite registry) against what
*actually* exists in docker, and emit corrective actions to bring
the two back in sync. Replaces the old "cleanup_orphans_at_startup"
sweep, which only swept containers and missed cases like:

- DB row says running but the container was OOM-killed → user keeps
  hitting a 502 forever because nothing notices.
- sandbox-host crashed mid-provision (DB has nothing) but the
  container actually started → orphan eats resources.
- network has only sandbox-host attached (sandbox container died)
  → next provision can't recreate the same name.

Design split:

- `plan(rows, containers, networks, self_name)` → list[ReconcileAction]
  is **pure**: no I/O, no docker SDK calls. Fully unit-testable from
  the decision table in the runbook.

- `apply(action, ...)` performs the action via the docker client
  and the registry. Errors are logged; one failed action doesn't
  block the rest of the plan.

Run at startup AND periodically (Phase 5 wires the loop). Both
entries call the same plan/apply pair so behaviour is identical.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable, Literal

import docker
from docker.errors import APIError, NotFound

from .provisioning import _best_effort_cleanup, _container_name, _network_name
from .settings import SandboxHostSettings
from .state import SandboxRecord, SandboxRegistry

log = logging.getLogger(__name__)


# What the reconciler sees about a docker container we own.
@dataclass(frozen=True)
class ContainerView:
    name: str            # td-sandbox-<task_id> [-g<gen>]
    task_id: str         # from label ccpt.sandbox.task_id
    running: bool        # container.status == "running"


# What the reconciler sees about a docker network we own.
@dataclass(frozen=True)
class NetworkView:
    name: str            # td-sandbox-net-*
    # Container names attached to this network (including ourselves).
    attached_names: tuple[str, ...]


ActionKind = Literal[
    "mark_stopped",        # update DB status; emit task.event
    "mark_running",        # provisioning crash but container is up
    "mark_error",          # provisioning crash with no container
    "delete_row",          # DB row no longer relevant
    "teardown_container",  # cleanup container (and its network)
    "remove_network",      # detach self + remove an empty network
]


@dataclass(frozen=True)
class ReconcileAction:
    kind: ActionKind
    task_id: str
    # Optional fields used by specific actions; None when N/A.
    container_name: str | None = None
    network_name: str | None = None
    reason: str = ""


def plan(
    *,
    rows: Iterable[SandboxRecord],
    containers: Iterable[ContainerView],
    networks: Iterable[NetworkView],
    self_container_name: str,
    network_prefix: str,
) -> list[ReconcileAction]:
    """Pure decision function. See the docstring at module top for the
    full table. We want to keep this readable rather than clever — each
    branch maps 1:1 to a row in the table."""
    actions: list[ReconcileAction] = []

    rows_by_id = {r.task_id: r for r in rows}
    containers_by_id = {c.task_id: c for c in containers}

    # ---- DB-driven branch: what should be true vs what is true ----
    for tid, row in rows_by_id.items():
        c = containers_by_id.get(tid)
        if row.status == "running":
            if c is None:
                # Container vanished (OOM, manual rm, crashed).
                actions.append(ReconcileAction(
                    kind="mark_stopped",
                    task_id=tid,
                    reason="container vanished",
                ))
            elif not c.running:
                # Container exists but stopped. Clean up + record.
                actions.append(ReconcileAction(
                    kind="teardown_container",
                    task_id=tid,
                    container_name=c.name,
                    network_name=row.network_name,
                    reason="container exited",
                ))
                actions.append(ReconcileAction(
                    kind="mark_stopped",
                    task_id=tid,
                    reason="container exited",
                ))
            # else: status running + container running → no-op.
        elif row.status == "provisioning":
            if c is None:
                actions.append(ReconcileAction(
                    kind="mark_error",
                    task_id=tid,
                    network_name=row.network_name,
                    reason="provision crashed pre-container",
                ))
            elif c.running:
                # Provision actually finished but the post-write crashed.
                actions.append(ReconcileAction(
                    kind="mark_running",
                    task_id=tid,
                    reason="container is up; finalising state",
                ))
            else:
                actions.append(ReconcileAction(
                    kind="teardown_container",
                    task_id=tid,
                    container_name=c.name,
                    network_name=row.network_name,
                    reason="provision crashed mid-start",
                ))
                actions.append(ReconcileAction(
                    kind="mark_error",
                    task_id=tid,
                    reason="provision crashed mid-start",
                ))
        elif row.status == "stopping":
            # User-initiated teardown that didn't finish.
            if c is not None:
                actions.append(ReconcileAction(
                    kind="teardown_container",
                    task_id=tid,
                    container_name=c.name,
                    network_name=row.network_name,
                    reason="finalising stopping",
                ))
            actions.append(ReconcileAction(
                kind="delete_row",
                task_id=tid,
                reason="stopping → stopped → row gone",
            ))
        elif row.status in ("stopped", "error"):
            # Terminal-ish rows: we keep the row only briefly for UI;
            # the reconciler's job is to make sure no docker resource
            # outlives them.
            if c is not None:
                actions.append(ReconcileAction(
                    kind="teardown_container",
                    task_id=tid,
                    container_name=c.name,
                    network_name=row.network_name,
                    reason="leftover container",
                ))
            actions.append(ReconcileAction(
                kind="delete_row",
                task_id=tid,
                reason="terminal state, prune row",
            ))
        # Any other status (unexpected): leave alone, log via apply.

    # ---- Docker-driven branch: orphans (container with no DB row) ----
    for tid, c in containers_by_id.items():
        if tid in rows_by_id:
            continue
        actions.append(ReconcileAction(
            kind="teardown_container",
            task_id=tid,
            container_name=c.name,
            network_name=f"{network_prefix}-{tid}",
            reason="orphan: container with no DB row",
        ))

    # ---- Network-driven branch: zombie networks ----
    # A sandbox network is a zombie when its only attached endpoint is
    # ourselves (sandbox-host) AND no live container exists for the
    # task it belongs to. Removal requires self-detach first.
    live_task_ids = {
        tid for tid, c in containers_by_id.items() if c.running
    }
    for n in networks:
        if not n.name.startswith(network_prefix):
            continue
        # Extract task_id from network name. Format guarantees prefix +
        # "-" + task_id (Phase 4 will add a generation suffix; this
        # branch tolerates either).
        task_id_part = n.name[len(network_prefix) + 1:]  # strip "<prefix>-"
        # Phase 4 will introduce "-g<N>" suffix — strip it here so we
        # match the docker container's task_id correctly. Without -g,
        # this is a no-op.
        # (Pure-function: we don't know yet which scheme is in play, so
        # be tolerant.)
        bare_task_id = task_id_part.rsplit("-g", 1)[0]
        if bare_task_id in live_task_ids:
            continue  # network is in use, leave it
        # Filter out self from "attached".
        others = [
            name for name in n.attached_names if name != self_container_name
        ]
        if others:
            # Some other container is still attached — refuse to touch.
            # Could happen if user attached something manually.
            continue
        actions.append(ReconcileAction(
            kind="remove_network",
            task_id=bare_task_id,
            network_name=n.name,
            reason="zombie network (self-only or empty)",
        ))

    return actions


# ---- Apply layer ----------------------------------------------------


async def apply(
    *,
    action: ReconcileAction,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    docker_client: docker.DockerClient,
    bus: object | None = None,  # WS bus, optional
) -> None:
    """Perform a single reconcile action. Errors are logged; the
    caller should continue with the next action regardless."""
    try:
        if action.kind == "mark_stopped":
            row = await registry.get(action.task_id)
            if row is not None:
                row.status = "stopped"
                row.error_message = action.reason or row.error_message
                await registry.add(row)
            log.info(
                "reconcile: marked %s stopped (%s)",
                action.task_id, action.reason,
            )
        elif action.kind == "mark_running":
            row = await registry.get(action.task_id)
            if row is not None:
                row.status = "running"
                row.error_message = None
                await registry.add(row)
            log.info("reconcile: marked %s running (%s)", action.task_id, action.reason)
        elif action.kind == "mark_error":
            row = await registry.get(action.task_id)
            if row is not None:
                row.status = "error"
                row.error_message = action.reason or "reconciler"
                await registry.add(row)
            log.info("reconcile: marked %s error (%s)", action.task_id, action.reason)
        elif action.kind == "delete_row":
            await registry.remove(action.task_id)
            log.info("reconcile: deleted row %s (%s)", action.task_id, action.reason)
        elif action.kind == "teardown_container":
            await _best_effort_cleanup(
                docker_client,
                action.container_name or _container_name(action.task_id),
                action.network_name
                or _network_name(settings.network_prefix, action.task_id),
                self_container_name=settings.self_container_name,
            )
            log.info(
                "reconcile: tore down %s container=%s (%s)",
                action.task_id, action.container_name, action.reason,
            )
        elif action.kind == "remove_network":
            assert action.network_name is not None
            try:
                n = await asyncio.to_thread(
                    docker_client.networks.get, action.network_name,
                )
                # Detach self if attached.
                if settings.self_container_name:
                    try:
                        await asyncio.to_thread(n.reload)
                        attached = n.attrs.get("Containers") or {}
                        self_attached = any(
                            (info or {}).get("Name") == settings.self_container_name
                            for info in attached.values()
                        )
                        if self_attached:
                            await asyncio.to_thread(
                                n.disconnect,
                                settings.self_container_name,
                                force=True,
                            )
                    except APIError as e:
                        log.warning(
                            "reconcile: self-detach from %s failed: %s",
                            action.network_name, e,
                        )
                await asyncio.to_thread(n.remove)
                log.info(
                    "reconcile: removed zombie network %s (%s)",
                    action.network_name, action.reason,
                )
            except NotFound:
                pass  # already gone — fine
        else:
            log.warning("reconcile: unknown action kind %s", action.kind)
    except Exception as e:  # noqa: BLE001 — never propagate; reconciler is best-effort
        log.warning(
            "reconcile: action %s on %s failed: %s",
            action.kind, action.task_id, e,
        )


# ---- Entry points ---------------------------------------------------


async def collect_state(
    *,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    docker_client: docker.DockerClient,
) -> tuple[
    list[SandboxRecord],
    list[ContainerView],
    list[NetworkView],
]:
    """Snapshot current state from registry + docker. Each list is
    independent so plan() can diff without re-querying."""
    rows = await registry.list_all()

    containers: list[ContainerView] = []
    try:
        cs = await asyncio.to_thread(
            docker_client.containers.list,
            all=True,
            filters={"label": "ccpt.sandbox.task_id"},
        )
        for c in cs:
            tid = (c.labels or {}).get("ccpt.sandbox.task_id", "")
            if not tid:
                continue
            containers.append(ContainerView(
                name=c.name or "",
                task_id=tid,
                running=(c.status == "running"),
            ))
    except APIError as e:
        log.warning("reconcile: docker containers.list failed: %s", e)

    networks: list[NetworkView] = []
    try:
        ns = await asyncio.to_thread(
            docker_client.networks.list,
            filters={"name": settings.network_prefix},
        )
        for n in ns:
            if not n.name.startswith(settings.network_prefix):
                continue
            try:
                await asyncio.to_thread(n.reload)
            except APIError:
                continue
            attached = n.attrs.get("Containers") or {}
            names = tuple(
                str((info or {}).get("Name", ""))
                for info in attached.values()
                if (info or {}).get("Name")
            )
            networks.append(NetworkView(name=n.name, attached_names=names))
    except APIError as e:
        log.warning("reconcile: docker networks.list failed: %s", e)

    return rows, containers, networks


async def reconcile_once(
    *,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    docker_client: docker.DockerClient | None = None,
    bus: object | None = None,
) -> dict[str, int]:
    """Run one full reconcile pass: collect, plan, apply each action.

    Returns a {action_kind: count} dict so callers (esp. the periodic
    loop) can log "what happened this tick" at a glance. An empty
    dict means the system was already consistent — no drift detected.
    """
    client = docker_client or docker.from_env()
    rows, containers, networks = await collect_state(
        settings=settings, registry=registry, docker_client=client,
    )
    actions = plan(
        rows=rows,
        containers=containers,
        networks=networks,
        self_container_name=settings.self_container_name,
        network_prefix=settings.network_prefix,
    )
    counts: dict[str, int] = {}
    for a in actions:
        counts[a.kind] = counts.get(a.kind, 0) + 1
        await apply(
            action=a,
            settings=settings,
            registry=registry,
            docker_client=client,
            bus=bus,
        )
    return counts


async def reconcile_loop(
    *,
    settings: SandboxHostSettings,
    registry: SandboxRegistry,
    stop_event: asyncio.Event,
    interval_seconds: float,
    bus: object | None = None,
) -> None:
    """Long-running task: run reconcile_once every interval_seconds.
    Exits cleanly when stop_event is set.

    Heals drift the per-task lock can't see: a sandbox container that
    got kill -9'd outside our knowledge, or a network that lingered
    after a docker daemon hiccup. With locking + idempotent provision
    (Phase 3) plus this loop, the steady-state error rate trends to
    zero — any out-of-band damage gets noticed within `interval`.
    """
    log.info(
        "reconcile loop starting (interval=%.0fs)", interval_seconds,
    )
    while not stop_event.is_set():
        try:
            counts = await reconcile_once(
                settings=settings, registry=registry, bus=bus,
            )
            if counts:
                log.info(
                    "reconcile tick: %s",
                    ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile tick error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return  # event set during wait → exit cleanly
        except asyncio.TimeoutError:
            pass  # normal interval — continue
    log.info("reconcile loop exiting")
