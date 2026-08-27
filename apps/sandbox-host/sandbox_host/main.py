"""sandbox-host FastAPI application.

Two API surfaces:

1. **Internal API** (called by core):
     POST /provision  → start a sandbox container for a task
     POST /stop       → stop + remove a sandbox container
     GET  /status     → current registry contents
     GET  /health     → liveness

2. **Public API** (called by Caddy as reverse-proxy upstream):
     ANY  /sandbox/{task_id}/{path:path}  → forward to the sandbox container

The proxy path also drives idle GC by updating last_request_at on
every passed-through request (see proxy.py).

Authentication: this service does NOT authenticate callers. Caddy
must terminate auth (forward_auth to core) BEFORE proxying here.
We trust our docker-compose network boundary.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel


def _guess_mime(path: Path) -> str:
    """Best-effort MIME for known viewer types. Defaults to
    application/octet-stream for unknown extensions."""
    suffix = path.suffix.lower()
    # Markdown isn't in the stdlib mimetypes; we want the frontend to
    # render it, not download.
    if suffix in (".md", ".markdown"):
        return "text/markdown; charset=utf-8"
    if suffix in (".csv", ".tsv"):
        return "text/csv; charset=utf-8"
    if suffix == ".json" or suffix == ".jsonl" or suffix == ".ndjson":
        return "application/json"
    if suffix in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java"):
        return "text/plain; charset=utf-8"
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"

from .detection import detect_outputs
from .gc import cleanup_orphans_at_startup, gc_loop  # noqa: F401 — kept for compat
from .lifecycle import LifecycleService
from .provisioning import (
    CapacityError,
    ProvisionError,
    provision,
    teardown,
)
from .proxy import make_proxy_client, proxy_request, proxy_websocket
from .reconciler import reconcile_loop, reconcile_once
from .settings import SandboxHostSettings
from .state import SandboxRegistry
from .workspace_gc import workspace_gc_loop

log = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# Body / response models at module level so FastAPI's body inference
# works correctly. (Inner-scope BaseModel subclasses got mis-parsed
# as query params.)
class ProvisionBody(BaseModel):
    task_id: str
    workspace_slug: str
    # P6.4: optional output_idx selecting which interactive output
    # from the manifest to launch. None / 0 = first interactive.
    output_idx: int | None = None


class ProvisionResponse(BaseModel):
    task_id: str
    host_port: int
    runtime: str
    image: str
    base_path: str


class StopBody(BaseModel):
    task_id: str


class RestoreBody(BaseModel):
    task_id: str
    workspace_slug: str
    archive_key: str


def create_app(settings: SandboxHostSettings | None = None) -> FastAPI:
    s = settings or SandboxHostSettings()  # type: ignore[call-arg]
    # P-H Phase 1: state lives at <work_dir>/.sandbox-host/state.db.
    # Same docker volume as worktrees, so a single bind-mount in
    # docker-compose covers both. Tests pass a tmp dir via settings.
    state_db_path = s.work_dir / ".sandbox-host" / "state.db"
    registry = SandboxRegistry(db_path=state_db_path)
    # P-H Phase 3: serialise per-task provision/teardown so user
    # double-clicks and rerun races don't collide.
    lifecycle = LifecycleService()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info(
            "sandbox-host starting; runtime=%s, max=%d, idle=%ds, work_dir=%s",
            s.container_runtime, s.max_concurrent, s.idle_seconds, s.work_dir,
        )

        # Per-app proxy client. Lives only as long as the app's loop.
        app.state.proxy_client = make_proxy_client()

        # P-H Phase 2: principled reconcile. Diffs the persisted DB
        # state against docker reality and emits corrective actions
        # for every kind of drift (vanished container, stale row,
        # zombie network, etc.). Replaces the older orphan sweep.
        try:
            counts = await reconcile_once(settings=s, registry=registry)
            if counts:
                log.info(
                    "startup reconcile: %s",
                    ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("startup reconcile skipped: %s", e)

        # Background loops:
        #   gc_loop reclaims idle sandbox containers (5 min default)
        #   workspace_gc_loop prunes stale worktrees (30 days)
        #   reconcile_loop heals drift between DB + docker every 60s
        stop_event = asyncio.Event()
        app.state.gc_stop_event = stop_event
        gc_task = asyncio.create_task(
            gc_loop(settings=s, registry=registry, stop_event=stop_event),
            name="sandbox-host-gc",
        )
        ws_gc_task = asyncio.create_task(
            workspace_gc_loop(
                settings=s, registry=registry, stop_event=stop_event,
            ),
            name="sandbox-host-workspace-gc",
        )
        reconcile_task = asyncio.create_task(
            reconcile_loop(
                settings=s,
                registry=registry,
                stop_event=stop_event,
                interval_seconds=s.reconciler_interval_seconds,
            ),
            name="sandbox-host-reconciler",
        )

        try:
            yield
        finally:
            stop_event.set()
            for t in (gc_task, ws_gc_task, reconcile_task):
                try:
                    await asyncio.wait_for(t, timeout=5)
                except asyncio.TimeoutError:
                    t.cancel()
            await app.state.proxy_client.aclose()
            log.info("sandbox-host shutting down")

    app = FastAPI(title="taskdeck-sandbox-host", lifespan=lifespan)
    app.state.settings = s
    app.state.registry = registry

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "sandboxes": len(registry),
            "max_concurrent": s.max_concurrent,
            "runtime": s.container_runtime,
        }

    @app.get("/status")
    async def status():
        records = await registry.list_all()
        return {
            "count": len(records),
            "max_concurrent": s.max_concurrent,
            "sandboxes": [
                {
                    "task_id": r.task_id,
                    "status": r.status,
                    "runtime": r.runtime,
                    "image": r.image,
                    "host_port": r.host_port,
                    "started_at": r.started_at.isoformat(),
                    "last_request_at": r.last_request_at.isoformat(),
                }
                for r in records
            ],
        }

    # ----- Internal API (called by core over docker-compose net) -----

    @app.post("/provision", response_model=ProvisionResponse)
    async def post_provision(body: ProvisionBody):
        workspace = s.work_dir / body.workspace_slug / "tasks" / body.task_id
        if not workspace.is_dir():
            raise HTTPException(
                404,
                "workspace no longer on disk — task was completed before "
                "workspace retention was enabled, or was pruned by the "
                "30-day LRU GC. Rerun the task to recreate the workspace, "
                "then try again.",
            )

        # P6.4: pick the interactive output the user asked for.
        outputs = detect_outputs(workspace)
        interactive_outputs = [o for o in outputs if o.kind == "interactive"]
        if not interactive_outputs:
            raise HTTPException(
                400,
                "no interactive output found. Workspace contains: "
                + ", ".join(f"{o.kind}({o.entry})" for o in outputs)
                + (
                    "" if outputs
                    else "(empty). Did the agent forget to write files?"
                ),
            )
        idx = body.output_idx or 0
        if idx >= len(interactive_outputs):
            raise HTTPException(
                400,
                f"output_idx={idx} out of range; have "
                f"{len(interactive_outputs)} interactive outputs",
            )
        chosen = interactive_outputs[idx]

        # Build a SandboxSpec from the chosen Output (provisioning
        # still uses SandboxSpec internally for now — refactor later).
        from .detection import SandboxSpec
        spec = SandboxSpec(
            runtime=chosen.runtime or "static",  # type: ignore[arg-type]
            image_key=chosen.runtime or "static",
            install_cmd=chosen.install_cmd,
            start_cmd=chosen.start_cmd or "nginx -g 'daemon off;'",
            port=chosen.port or 8080,
            source=chosen.source or "manifest",
        )

        try:
            record = await lifecycle.provision_idempotent(
                task_id=body.task_id,
                workspace=workspace,
                spec=spec,
                settings=s,
                registry=registry,
            )
        except CapacityError as e:
            raise HTTPException(429, str(e)) from e
        except ProvisionError as e:
            raise HTTPException(500, f"provision failed: {e}") from e

        # P6.4: if the chosen output isn't `/` (e.g. agent wrote
        # counter.html instead of index.html), extend base_path with
        # the entry so the frontend's window.open jumps directly to
        # the file rather than landing on a directory listing.
        # Static runtime entries are simple files; node/python
        # runtimes typically expose `/` so we leave those alone.
        open_path = record.base_path
        if (
            chosen.runtime == "static"
            and chosen.entry not in {"index.html", "index.htm", ""}
        ):
            open_path = record.base_path + chosen.entry

        return ProvisionResponse(
            task_id=record.task_id,
            host_port=record.host_port,
            runtime=record.runtime,
            image=record.image,
            base_path=open_path,
        )

    # ---- Manifest / file / tree (P6.4 viewers) ---------------------

    def _resolve_workspace(workspace_slug: str, task_id: str) -> Path:
        return s.work_dir / workspace_slug / "tasks" / task_id

    def _safe_resolve(workspace: Path, rel: str) -> Path:
        """Resolve `rel` inside `workspace`, blocking traversal.
        Raises HTTPException(400) on escape, 404 on missing."""
        # Refuse anything containing '..' even before resolve, to be
        # defensive against symlink shenanigans.
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise HTTPException(400, f"invalid path: {rel}")
        full = (workspace / rel).resolve()
        try:
            full.relative_to(workspace.resolve())
        except ValueError:
            raise HTTPException(400, f"path escapes workspace: {rel}") from None
        if not full.exists():
            raise HTTPException(404, f"not found: {rel}")
        return full

    @app.get("/manifest/{workspace_slug}/{task_id}")
    async def get_manifest(workspace_slug: str, task_id: str):
        """Return list of outputs for a task workspace."""
        workspace = _resolve_workspace(workspace_slug, task_id)
        if not workspace.is_dir():
            raise HTTPException(
                404,
                "workspace no longer on disk — task was completed before "
                "workspace retention was enabled, or was pruned by the "
                "30-day LRU GC. Rerun the task to recreate the workspace.",
            )
        outputs = detect_outputs(workspace)
        return {
            "task_id": task_id,
            "outputs": [
                {
                    "kind": o.kind,
                    "entry": o.entry,
                    "label": o.label,
                    "source": o.source,
                    "runtime": o.runtime,
                    "port": o.port,
                }
                for o in outputs
            ],
        }

    @app.get("/file/{workspace_slug}/{task_id}/{rel_path:path}")
    async def get_file(workspace_slug: str, task_id: str, rel_path: str):
        """Read a workspace file. Returns raw content with the right
        MIME type so the frontend can render it inline.

        Caps responses at 5 MB to keep the drawer responsive — anything
        larger is presumably a binary the user should download via the
        archive kind."""
        workspace = _resolve_workspace(workspace_slug, task_id)
        if not workspace.is_dir():
            raise HTTPException(404, "workspace not found")
        full = _safe_resolve(workspace, rel_path)
        if full.is_dir():
            raise HTTPException(400, f"is a directory: {rel_path}")
        size = full.stat().st_size
        max_bytes = 5 * 1024 * 1024
        if size > max_bytes:
            raise HTTPException(
                413, f"file too large ({size} bytes); max {max_bytes}",
            )
        content_type = _guess_mime(full)
        # Always emit a filename so browsers (or <a download>) save
        # under the agent's actual filename instead of "file" (the
        # last URL segment) or a guessed name. RFC 6266 / 5987
        # filename* keeps non-ASCII names (CJK) intact across browsers.
        from urllib.parse import quote
        ascii_safe = full.name.encode("ascii", "ignore").decode("ascii") or "download"
        encoded = quote(full.name)
        disposition = (
            f"inline; filename=\"{ascii_safe}\"; filename*=UTF-8''{encoded}"
        )
        return FileResponse(
            full,
            media_type=content_type,
            filename=full.name,
            headers={"Content-Disposition": disposition},
        )

    @app.get("/tree/{workspace_slug}/{task_id}")
    async def get_tree(workspace_slug: str, task_id: str, max_depth: int = 4):
        """Return the workspace directory tree as a flat list of
        {path, kind, size}. max_depth keeps blast radius bounded."""
        workspace = _resolve_workspace(workspace_slug, task_id)
        if not workspace.is_dir():
            raise HTTPException(404, "workspace not found")
        entries: list[dict] = []
        skip = {"node_modules", ".git", "__pycache__", ".venv", "venv"}

        def walk(p: Path, depth: int):
            if depth > max_depth:
                return
            try:
                children = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
            except OSError:
                return
            for child in children:
                if child.name in skip:
                    continue
                rel = child.relative_to(workspace).as_posix()
                if child.is_dir():
                    entries.append({"path": rel, "kind": "dir"})
                    walk(child, depth + 1)
                else:
                    try:
                        size = child.stat().st_size
                    except OSError:
                        size = 0
                    entries.append({"path": rel, "kind": "file", "size": size})

        walk(workspace, 0)
        return {"task_id": task_id, "entries": entries}

    @app.post("/stop")
    async def post_stop(body: StopBody):
        found = await lifecycle.teardown_idempotent(
            task_id=body.task_id, settings=s, registry=registry,
        )
        return {"found": found}

    @app.post("/restore")
    async def post_restore(body: RestoreBody):
        """P6.3.7 cold archive: pull the tar.gz back from S3 and
        extract under work_dir. Called by core's start_sandbox flow
        when /provision has reported the workspace is missing AND the
        task has a non-NULL archive_key. Synchronous; returns when the
        workspace is back on disk.
        """
        from .archive import ArchiveError, restore_workspace
        try:
            workspace_dir = await restore_workspace(
                settings=s,
                workspace_slug=body.workspace_slug,
                task_id=body.task_id,
                archive_key=body.archive_key,
            )
        except ArchiveError as e:
            raise HTTPException(500, f"restore failed: {e}") from e
        return {
            "task_id": body.task_id,
            "workspace_dir": str(workspace_dir),
            "restored": True,
        }

    # ----- Public API (called by Caddy) -----

    @app.api_route(
        "/sandbox/{task_id}/{rel_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def sandbox_proxy(task_id: str, rel_path: str, request: Request):
        return await proxy_request(
            request=request, task_id=task_id, rel_path=rel_path,
            registry=registry, client=request.app.state.proxy_client,
        )

    # Bare /sandbox/{task_id}/ (no trailing path) — same handler.
    @app.api_route(
        "/sandbox/{task_id}/",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def sandbox_proxy_root(task_id: str, request: Request):
        return await proxy_request(
            request=request, task_id=task_id, rel_path="",
            registry=registry, client=request.app.state.proxy_client,
        )

    # WebSocket proxy. Same path shape as the HTTP proxy; FastAPI
    # routes ws and http to separate handlers even when they share a
    # path. Caddy's reverse_proxy forwards the Upgrade header
    # automatically so this works through the full chain.
    @app.websocket("/sandbox/{task_id}/{rel_path:path}")
    async def sandbox_ws(websocket: WebSocket, task_id: str, rel_path: str):
        await proxy_websocket(
            websocket=websocket, task_id=task_id, rel_path=rel_path,
            registry=registry,
        )

    return app


def run() -> None:
    """Entrypoint for the console script."""
    import uvicorn
    s = SandboxHostSettings()  # type: ignore[call-arg]
    uvicorn.run(
        "sandbox_host.main:create_app",
        host=s.host,
        port=s.port,
        factory=True,
        log_level="info",
    )


if __name__ == "__main__":
    run()
