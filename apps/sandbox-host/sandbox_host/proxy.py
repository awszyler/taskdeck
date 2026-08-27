"""HTTP + WebSocket reverse proxy from sandbox-host to per-task containers.

Caddy hits us at:
  ANY /sandbox/<task_id>/<path>?<qs>
            └── matched here, lookup task_id → host_port from registry,
                strip the prefix, forward to 127.0.0.1:<host_port>/<path>?<qs>

Every successful HTTP request bumps SandboxRecord.last_request_at —
that's what keeps idle GC honest. WS connections bump on connect and
on every frame in either direction, so a quiet long-lived WS keeps
its sandbox alive as long as either peer is sending.

What we strip from HTTP:
  - "Host" header is rewritten to the upstream host so cookies don't
    accidentally bind to sandbox-host's hostname.
  - "Connection: close" is added to short-circuit any keepalive
    games between Caddy/sandbox-host/container; httpx handles
    pooling internally.

What we strip from WS:
  - The WebSocket handshake is owned by starlette/Caddy on the inbound
    side and the websockets lib on the outbound side; we never see the
    `Sec-WebSocket-*` / `Upgrade` headers as application-level data, so
    nothing to strip — we just open both ends and pump frames.
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from typing import AsyncIterator

import httpx
import websockets
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse

from .state import SandboxRegistry

log = logging.getLogger(__name__)


# P-H Phase 7: cache "is upstream alive?" results for a short window.
# Without the cache every request would do a TCP connect first, which
# is fine when the sandbox is alive but adds 1-2ms × every static asset
# load. With the cache, we probe at most once every PROBE_TTL_SECONDS
# per task. False results are cached for a shorter window so a freshly-
# started sandbox doesn't stay 502 longer than necessary.
_PROBE_TTL_OK_S = 10.0
_PROBE_TTL_FAIL_S = 1.0
_PROBE_TIMEOUT_S = 0.3

# Map: task_id -> (alive: bool, expires_at: float)
_PROBE_CACHE: dict[str, tuple[bool, float]] = {}


async def _probe_upstream(host: str, port: int) -> bool:
    """Cheap TCP connect probe. Returns True iff upstream accepts."""
    def _connect() -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(_PROBE_TIMEOUT_S)
            try:
                s.connect((host, port))
                return True
            except OSError:
                return False
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_connect),
            timeout=_PROBE_TIMEOUT_S + 0.5,
        )
    except asyncio.TimeoutError:
        return False


async def _probe_or_cached(task_id: str, host: str, port: int) -> bool:
    now = time.monotonic()
    cached = _PROBE_CACHE.get(task_id)
    if cached is not None and cached[1] > now:
        return cached[0]
    alive = await _probe_upstream(host, port)
    ttl = _PROBE_TTL_OK_S if alive else _PROBE_TTL_FAIL_S
    _PROBE_CACHE[task_id] = (alive, now + ttl)
    return alive


def _invalidate_probe(task_id: str) -> None:
    """Drop the cache entry — used when teardown happens elsewhere
    so the next request doesn't serve a stale 'alive' verdict."""
    _PROBE_CACHE.pop(task_id, None)


# HTML response bodies under text/html get attribute-level rewrites so
# absolute paths in agent-generated HTML keep working under the
# /sandbox/<task_id>/ prefix. Only attribute values that start with a
# single `/` followed by a non-`/` are rewritten — `//cdn.example.com`
# (protocol-relative) and full URLs are left alone, and only inside
# href/src/action/formaction quoted attributes (no inline-string scan).
#
# Limit: the rewrite is intentionally narrow. fetch("/api/x") and
# CSS url(/...) are NOT rewritten — broadening the regex risks
# corrupting JSON / JS string literals that look like paths.
# Agents that need those should use relative paths.
_HTML_PATH_ATTR_RE = re.compile(
    rb'(?P<attr>\b(?:href|src|action|formaction))'
    rb'(?P<eq>\s*=\s*)'
    rb'(?P<quote>["\'])'
    rb'(?P<path>/(?!/)[^"\'#?\s]*)',
    re.IGNORECASE,
)
# Cap on buffered body size for HTML rewriting. Larger bodies fall
# back to streaming (no rewrite) — pragmatic guard against memory
# blowup on a misclassified large download.
_HTML_REWRITE_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB


# Headers that must NOT be forwarded blindly. RFC 7230 §6.1 hop-by-hop +
# a couple we manage ourselves.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    # Set by httpx; don't forward stale values from the client.
    "host",
    "content-length",
}


def _scrub_request_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


def _scrub_response_headers(headers: httpx.Headers) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out[k] = v
    return out


def _is_html_response(headers: httpx.Headers) -> bool:
    """True iff the upstream is sending HTML we should rewrite.

    Only `text/html` (with optional charset) qualifies. JSON / images /
    binary / SSE / arbitrary text/* all skip rewriting. Streamed
    transfer-encoding also skips: we'd have to either buffer the full
    stream (defeats SSE) or re-chunk after rewrite.
    """
    ct = headers.get("content-type", "").lower()
    if not ct.startswith("text/html"):
        return False
    # Chunked / unknown-length bodies go through unmodified — better
    # to under-rewrite than to break long-poll / SSE-misclassified
    # streams by buffering them.
    if headers.get("transfer-encoding", "").lower() == "chunked":
        return False
    return True


def _rewrite_html_body(body: bytes, base_path: str) -> bytes:
    """Prepend `base_path` to absolute-path attributes in HTML.

    base_path is the no-trailing-slash form, e.g. `/sandbox/<task_id>`.
    The original `/foo` becomes `/sandbox/<task_id>/foo`.
    """
    prefix = base_path.encode("utf-8")
    return _HTML_PATH_ATTR_RE.sub(
        lambda m: (
            m.group("attr") + m.group("eq") + m.group("quote")
            + prefix + m.group("path")
        ),
        body,
    )


# httpx.AsyncClient holds open TCP connections + is bound to the
# event loop that created it. We attach one client *per FastAPI app
# instance* (lifespan-scoped) so tests that recreate the app/loop
# don't reuse a stale client. main.py wires this via app.state.
def make_proxy_client() -> httpx.AsyncClient:
    """Build a fresh httpx client for proxying. Caller owns it and
    must aclose() during shutdown."""
    return httpx.AsyncClient(
        # No timeout on read/write — long-lived SSE / large bodies
        # must work. Connect is short to fail fast if upstream died.
        timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0),
        follow_redirects=False,
    )


async def proxy_request(
    *,
    request: Request,
    task_id: str,
    rel_path: str,
    registry: SandboxRegistry,
    client: httpx.AsyncClient,
) -> StreamingResponse:
    """Forward a request to the sandbox container for task_id.

    rel_path is the path *inside* the sandbox (i.e. without the
    /sandbox/<task_id>/ prefix). May be empty (root request).
    """
    record = await registry.get(task_id)
    if record is None or record.status != "running":
        raise HTTPException(404, f"sandbox {task_id} not running")

    # P-H Phase 7: TCP-probe the upstream before paying for a full
    # HTTP roundtrip. The DB row may say "running" while the
    # container actually got OOM-killed since the last reconcile
    # tick. Probing first means the user sees a fast 502 + clean
    # state instead of a hung tab.
    if not await _probe_or_cached(
        task_id, record.container_name, record.internal_port,
    ):
        log.info(
            "proxy: upstream %s:%s unreachable, marking stopped",
            record.container_name, record.internal_port,
        )
        # Update DB so subsequent requests / the kanban see the
        # correct status. Reconciler will pick up the dead container/
        # network on its next tick.
        record.status = "stopped"
        record.error_message = "upstream unreachable on probe"
        await registry.add(record)
        _invalidate_probe(task_id)
        raise HTTPException(
            502, f"sandbox {task_id} upstream unreachable",
        )

    # Bump idle clock for GC.
    await registry.touch(task_id)

    # Build upstream URL. We talk to the sandbox container by its
    # docker DNS name on the per-sandbox network (sandbox-host has
    # been attached to that network at provision time). For the
    # dev/test path where sandbox-host runs on the host loopback,
    # tests inject `record.container_name = "127.0.0.1"`.
    upstream = (
        f"http://{record.container_name}:{record.internal_port}"
        f"/{rel_path.lstrip('/')}"
    )
    qs = request.url.query
    if qs:
        upstream = f"{upstream}?{qs}"

    # Headers + body passthrough.
    headers = _scrub_request_headers(dict(request.headers))
    # Some apps rely on X-Forwarded-* for routing/links. Set them.
    headers["X-Forwarded-Host"] = request.headers.get("host", "")
    headers["X-Forwarded-Proto"] = request.url.scheme or "http"
    headers["X-Forwarded-For"] = request.client.host if request.client else ""

    body = await request.body()
    method = request.method

    # Use streaming send so large bodies / SSE work both ways.
    upstream_req = client.build_request(
        method=method, url=upstream, headers=headers, content=body or None,
    )
    upstream_resp = await client.send(upstream_req, stream=True)

    # HTML branch: buffer, rewrite absolute paths, return a fixed-size
    # response. Bodies larger than the cap fall through to streaming —
    # keeps misclassified large downloads from blowing memory.
    if _is_html_response(upstream_resp.headers):
        try:
            body = await upstream_resp.aread()
        finally:
            await upstream_resp.aclose()

        if len(body) <= _HTML_REWRITE_MAX_BYTES:
            base_path = f"/sandbox/{task_id}"  # no trailing slash
            body = _rewrite_html_body(body, base_path)
            headers = _scrub_response_headers(upstream_resp.headers)
            # Length changed; let the framework recompute.
            headers.pop("content-length", None)
            return Response(
                content=body,
                status_code=upstream_resp.status_code,
                headers=headers,
                media_type=upstream_resp.headers.get("content-type"),
            )
        # Oversize HTML: serve as-is via a single-shot Response (we
        # already aread'd the body, can't re-stream).
        return Response(
            content=body,
            status_code=upstream_resp.status_code,
            headers=_scrub_response_headers(upstream_resp.headers),
            media_type=upstream_resp.headers.get("content-type"),
        )

    # Non-HTML: stream as before. Critical for SSE, large binaries,
    # JSON APIs, attachment downloads (Content-Disposition path).
    async def stream_body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_resp.status_code,
        headers=_scrub_response_headers(upstream_resp.headers),
        media_type=upstream_resp.headers.get("content-type"),
    )


async def proxy_websocket(
    *,
    websocket: WebSocket,
    task_id: str,
    rel_path: str,
    registry: SandboxRegistry,
) -> None:
    """Bidirectional WebSocket proxy for /sandbox/<task_id>/<rel_path>.

    Accept the client handshake, open a backend ws to the sandbox
    container, then pump frames in both directions until either side
    closes. Closes are propagated; cancellation propagates too via
    asyncio.gather wait=FIRST_COMPLETED.

    Like the HTTP path, we 404 if the sandbox isn't running. We do NOT
    do any frame inspection or rewriting — frames are passed through as
    text or bytes verbatim.
    """
    record = await registry.get(task_id)
    if record is None or record.status != "running":
        # WebSocket close codes use 1008 for "policy violation"; 1011
        # ("internal error") is closer to "we just don't have it".
        await websocket.close(code=1011, reason=f"sandbox {task_id} not running")
        return

    upstream_url = (
        f"ws://{record.container_name}:{record.internal_port}"
        f"/{rel_path.lstrip('/')}"
    )
    qs = websocket.url.query
    if qs:
        upstream_url = f"{upstream_url}?{qs}"

    await websocket.accept()
    await registry.touch(task_id)

    try:
        async with websockets.connect(upstream_url) as upstream:
            await _pump_frames(websocket, upstream, registry, task_id)
    except (websockets.InvalidURI, websockets.InvalidHandshake) as e:
        log.warning("ws upstream %s rejected handshake: %s", upstream_url, e)
        await websocket.close(code=1011, reason="upstream handshake failed")
    except OSError as e:
        log.warning("ws upstream %s unreachable: %s", upstream_url, e)
        await websocket.close(code=1011, reason="upstream unreachable")
    except WebSocketDisconnect:
        # Client disconnected — `async with` already cleaned up upstream.
        pass


async def _pump_frames(
    client: WebSocket,
    upstream: websockets.ClientConnection,
    registry: SandboxRegistry,
    task_id: str,
) -> None:
    """Run the two directions concurrently. First side to close wins;
    cancel the other and propagate the close."""

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await client.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                # starlette gives us either text or bytes per frame.
                if (data := msg.get("text")) is not None:
                    await upstream.send(data)
                elif (data := msg.get("bytes")) is not None:
                    await upstream.send(data)
                await registry.touch(task_id)
        except (WebSocketDisconnect, websockets.ConnectionClosed):
            return

    async def upstream_to_client() -> None:
        try:
            async for msg in upstream:
                if isinstance(msg, str):
                    await client.send_text(msg)
                else:
                    await client.send_bytes(msg)
                await registry.touch(task_id)
        except (WebSocketDisconnect, websockets.ConnectionClosed):
            return

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        # First completer ends the session — cancel the survivor so we
        # don't leak the half-open direction.
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        # Drain cancellations so they don't surface as unhandled in tests.
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        # Best-effort close on both sides; ignore errors if already closed.
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await client.close()
        except Exception:
            pass
