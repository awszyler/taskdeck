"""Sandbox API (P6.3).

Three user-facing endpoints + one Caddy auth helper:

  POST /api/v1/sandbox/{task_id}/start
       Provision a sandbox for the task. Calls into sandbox-host
       over the docker-compose internal network. Synchronous —
       returns when the user app is ready (or after timeout).

  POST /api/v1/sandbox/{task_id}/stop
       Tear down the sandbox. Idempotent.

  GET  /api/v1/sandbox/{task_id}/status
       Current sandbox state from our DB row.

  GET  /api/v1/sandbox/auth/{task_id}
       Caddy `forward_auth` target. Returns 200 if the requester
       has a valid session cookie AND is a member of the task's
       workspace. Returns 401/403 otherwise. Validates only auth
       — does NOT check that the sandbox is actually running.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID  # noqa: TCH003

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from taskdeck_core.api.tasks import PrincipalDep, SessionDep
from taskdeck_core.auth.memberships import get_visible_workspace_ids
from taskdeck_core.db.models import Sandbox, Task, Workspace

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sandbox", tags=["sandbox"])


# Statuses from which a user is allowed to launch a sandbox. These
# represent "the agent has produced something to look at".
_SANDBOXABLE = frozenset({"done", "in_review", "failed", "cancelled"})


# ------- Response models ----------------------------------------------


class SandboxStatus(BaseModel):
    task_id: UUID
    status: str
    host_port: int | None
    runtime: str | None
    base_path: str | None
    started_at: datetime | None
    stopped_at: datetime | None
    error_message: str | None


class StartResponse(BaseModel):
    task_id: UUID
    base_path: str
    runtime: str


class StopResponse(BaseModel):
    found: bool


# ------- Helpers ------------------------------------------------------


async def _verify_workspace_access(
    session, principal, task_id: UUID,
) -> Task:
    """Resolve task + check the user can access its workspace.
    404 (not 403) on cross-workspace to avoid leaking task existence."""
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and task.workspace_id not in visible:
        raise HTTPException(404, "task not found")
    return task


async def _get_workspace_slug(session, workspace_id: UUID) -> str:
    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(500, "task references missing workspace")
    return ws.slug


def _settings(request: Request):
    s = getattr(request.app.state, "settings", None)
    if s is None:
        raise HTTPException(500, "core settings not initialized")
    return s


# ------- Endpoints ----------------------------------------------------


@router.post("/{task_id}/start", response_model=StartResponse)
async def start_sandbox(
    task_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
    output_idx: int = 0,
) -> StartResponse:
    task = await _verify_workspace_access(session, principal, task_id)
    if task.status not in _SANDBOXABLE:
        raise HTTPException(
            409,
            f"cannot start sandbox for task in status '{task.status}'; "
            f"allowed: {sorted(_SANDBOXABLE)}",
        )

    workspace_slug = await _get_workspace_slug(session, task.workspace_id)
    settings = _settings(request)

    # Upsert a row in `sandboxes` showing provisioning state. We use
    # a get-or-create pattern; if the same task is restarted, we
    # update the existing row.
    now = datetime.now(UTC)
    sb = await session.get(Sandbox, task_id)
    if sb is None:
        sb = Sandbox(
            task_id=task_id,
            status="provisioning",
            created_at=now,
            updated_at=now,
        )
        session.add(sb)
    else:
        sb.status = "provisioning"
        sb.error_message = None
        sb.updated_at = now
    await session.flush()
    await session.commit()

    # Call sandbox-host. This blocks until the user app is ready
    # (typical 3-5s for static, 5-15s for node/python with install).
    timeout = httpx.Timeout(
        connect=5.0,
        read=settings.sandbox_provision_timeout,
        write=10.0,
        pool=5.0,
    )

    async def _provision_once(client: httpx.AsyncClient):
        return await client.post(
            f"{settings.sandbox_host_url}/provision",
            json={
                "task_id": str(task_id),
                "workspace_slug": workspace_slug,
                "output_idx": output_idx,
            },
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await _provision_once(client)
            # P6.3.7 reactivate: if sandbox-host says workspace is gone
            # (404) but task has an archive_key, pull the tarball back
            # from S3 via /restore and retry once. This is the only
            # path that auto-recovers from workspace_retention_days.
            if (
                resp.status_code == 404
                and task.archive_key
            ):
                log.info(
                    "start_sandbox: workspace missing for %s, restoring "
                    "archive %s", task_id, task.archive_key,
                )
                restore_resp = await client.post(
                    f"{settings.sandbox_host_url}/restore",
                    json={
                        "task_id": str(task_id),
                        "workspace_slug": workspace_slug,
                        "archive_key": task.archive_key,
                    },
                )
                if not restore_resp.is_success:
                    sb.status = "error"
                    sb.error_message = (
                        f"archive restore failed: {restore_resp.text[:300]}"
                    )
                    sb.updated_at = datetime.now(UTC)
                    await session.commit()
                    raise HTTPException(
                        500,
                        f"archive restore failed: {restore_resp.text[:300]}",
                    )
                # Retry provision now that workspace is back on disk.
                resp = await _provision_once(client)
    except httpx.HTTPError as e:
        sb.status = "error"
        sb.error_message = f"sandbox-host unreachable: {e}"
        sb.updated_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(502, f"sandbox-host unreachable: {e}") from e

    if resp.status_code == 429:
        sb.status = "error"
        sb.error_message = "max concurrent sandboxes reached"
        sb.updated_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(429, "max concurrent sandboxes reached")

    if not resp.is_success:
        detail = resp.text[:500]
        sb.status = "error"
        sb.error_message = detail
        sb.updated_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(
            500, f"sandbox provisioning failed: {detail}",
        )

    body = resp.json()
    sb.status = "running"
    sb.host_port = body["host_port"]
    sb.runtime = body["runtime"]
    sb.base_path = body["base_path"]
    sb.started_at = datetime.now(UTC)
    sb.stopped_at = None
    sb.error_message = None
    sb.updated_at = sb.started_at
    await session.commit()

    return StartResponse(
        task_id=task_id, base_path=sb.base_path, runtime=sb.runtime,
    )


@router.post("/{task_id}/stop", response_model=StopResponse)
async def stop_sandbox(
    task_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> StopResponse:
    await _verify_workspace_access(session, principal, task_id)
    settings = _settings(request)

    found = False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.sandbox_host_url}/stop",
                json={"task_id": str(task_id)},
            )
        if resp.is_success:
            found = bool(resp.json().get("found"))
    except httpx.HTTPError as e:
        log.warning("stop_sandbox: sandbox-host call failed: %s", e)
        # Best-effort: even if the host is unreachable, mark our DB
        # row as stopped so the UI doesn't show "running" forever.

    sb = await session.get(Sandbox, task_id)
    if sb is not None:
        sb.status = "stopped"
        sb.stopped_at = datetime.now(UTC)
        sb.updated_at = sb.stopped_at
        await session.commit()

    return StopResponse(found=found)


@router.get("/{task_id}/status", response_model=SandboxStatus)
async def sandbox_status(
    task_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> SandboxStatus:
    await _verify_workspace_access(session, principal, task_id)
    sb = await session.get(Sandbox, task_id)
    if sb is None:
        # Synthesize a "not provisioned" status rather than 404 — UI
        # uses this to decide whether to show "Open sandbox" or
        # "Sandbox not running".
        return SandboxStatus(
            task_id=task_id,
            status="not_provisioned",
            host_port=None,
            runtime=None,
            base_path=None,
            started_at=None,
            stopped_at=None,
            error_message=None,
        )
    return SandboxStatus(
        task_id=task_id,
        status=sb.status,
        host_port=sb.host_port,
        runtime=sb.runtime,
        base_path=sb.base_path,
        started_at=sb.started_at,
        stopped_at=sb.stopped_at,
        error_message=sb.error_message,
    )


@router.get("/{task_id}/manifest")
async def sandbox_manifest(
    task_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
):
    """Proxy to sandbox-host's /manifest, after workspace ACL check.
    Returns the list of outputs the UI uses to populate ⋯ menu."""
    task = await _verify_workspace_access(session, principal, task_id)
    workspace_slug = await _get_workspace_slug(session, task.workspace_id)
    settings = _settings(request)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.sandbox_host_url}"
                f"/manifest/{workspace_slug}/{task_id}",
            )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"sandbox-host unreachable: {e}") from e
    if resp.status_code == 404:
        # Workspace pruned / never retained. UI surfaces a message.
        raise HTTPException(404, resp.json().get("detail", "workspace not found"))
    if not resp.is_success:
        raise HTTPException(500, f"manifest fetch failed: {resp.text[:200]}")
    return resp.json()


@router.get("/{task_id}/file")
async def sandbox_file(
    task_id: UUID,
    path: str,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
):
    """Proxy to sandbox-host's /file. Streams the file body through.
    Used by drawer viewers (markdown, code, image, csv)."""
    task = await _verify_workspace_access(session, principal, task_id)
    workspace_slug = await _get_workspace_slug(session, task.workspace_id)
    settings = _settings(request)

    upstream = (
        f"{settings.sandbox_host_url}"
        f"/file/{workspace_slug}/{task_id}/{path.lstrip('/')}"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(upstream)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"sandbox-host unreachable: {e}") from e
    if not resp.is_success:
        raise HTTPException(resp.status_code, resp.text[:200])
    from fastapi.responses import Response as FastAPIResponse
    # Forward Content-Disposition so the browser saves the file with
    # the agent's filename instead of a generic "file" or guessed
    # name from the query string.
    forward_headers = {}
    cd = resp.headers.get("content-disposition")
    if cd:
        forward_headers["Content-Disposition"] = cd
    return FastAPIResponse(
        content=resp.content,
        media_type=resp.headers.get("content-type"),
        headers=forward_headers,
    )


@router.get("/{task_id}/tree")
async def sandbox_tree(
    task_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
):
    """Proxy to sandbox-host's /tree. Drawer file-tree viewer."""
    task = await _verify_workspace_access(session, principal, task_id)
    workspace_slug = await _get_workspace_slug(session, task.workspace_id)
    settings = _settings(request)

    upstream = (
        f"{settings.sandbox_host_url}/tree/{workspace_slug}/{task_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(upstream)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"sandbox-host unreachable: {e}") from e
    if not resp.is_success:
        raise HTTPException(resp.status_code, resp.text[:200])
    return resp.json()


@router.get("/auth/{task_id}")
async def sandbox_auth_check(
    task_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
):
    """forward_auth target. Returns 200 (with empty body) on pass.

    Caddy decides what to do based on status code only, so we don't
    need to return any payload. We do NOT check sandbox running
    state — the proxied request to sandbox-host will return 404 if
    it's not, and that's a clearer error than us second-guessing it.

    Performance: 1 SQL query (workspace ACL via task lookup).
    """
    # require_user is implied by PrincipalDep when auth_mode is cognito.
    # Reuse the existing access check (404s on cross-workspace).
    await _verify_workspace_access(session, principal, task_id)
    return {"ok": True}
