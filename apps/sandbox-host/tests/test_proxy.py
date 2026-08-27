"""Tests for the reverse proxy.

Strategy: spin up a small "fake upstream" FastAPI server on a random
local port, register it as a SandboxRecord, and hit sandbox-host's
/sandbox/<id>/* through TestClient. That gives us a real end-to-end
HTTP roundtrip without docker.

Note: TestClient must be used as a context manager (`with TestClient(...)`)
so the app's lifespan runs and `app.state.proxy_client` gets created.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sandbox_host.main import create_app
from sandbox_host.settings import SandboxHostSettings
from sandbox_host.state import SandboxRecord


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@contextmanager
def _running_upstream(app: FastAPI):
    """Run a uvicorn server in a background thread on a random port."""
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve()), daemon=True,
    )
    thread.start()

    # Wait for the server to bind.
    for _ in range(50):
        try:
            sock = socket.socket()
            sock.settimeout(0.2)
            sock.connect(("127.0.0.1", port))
            sock.close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError(f"upstream did not bind on :{port}")

    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=3)


def _fake_upstream() -> FastAPI:
    """Mini server that echoes path/method/query back."""
    from fastapi.responses import HTMLResponse, PlainTextResponse
    app = FastAPI()

    @app.get("/page.html", response_class=HTMLResponse)
    async def html_page():
        # Mix of cases the rewriter MUST handle, MUST NOT touch, and
        # MUST leave alone (protocol-relative + full URLs).
        return (
            '<!doctype html><html><head>'
            '<link rel="stylesheet" href="/style.css">'
            '<script src="/app.js"></script>'
            '</head><body>'
            '<a href="/about">about</a>'
            '<a href="//cdn.example.com/x.js">cdn</a>'  # protocol-relative
            '<a href="https://example.com/x">abs</a>'
            '<a href="./relative.html">rel</a>'
            '<form action="/submit" method="post"></form>'
            # An inline string that LOOKS like a path but isn't an
            # attribute — must NOT be rewritten.
            '<script>fetch("/api/x");</script>'
            '</body></html>'
        )

    @app.get("/json-with-paths")
    async def json_paths():
        # Regression guard: JSON whose VALUES contain "/foo" must not
        # be touched even if they sit next to keys called "href".
        return {"href": "/should-stay", "data": "/x/y"}

    @app.get("/text-plain", response_class=PlainTextResponse)
    async def text_plain():
        # text/plain != text/html; must skip rewrite entirely.
        return '<a href="/keep-me">x</a>'

    @app.get("/")
    async def root():
        return {"path": "/", "method": "GET"}

    @app.get("/foo/bar")
    async def foo_bar(request: Request):
        return {
            "path": request.url.path,
            "query": dict(request.query_params),
            "method": "GET",
        }

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {
            "method": "POST",
            "body": body.decode(),
            "ct": request.headers.get("content-type"),
        }

    @app.get("/headers")
    async def headers(request: Request):
        return {
            "x_forwarded_host": request.headers.get("x-forwarded-host"),
            "x_forwarded_proto": request.headers.get("x-forwarded-proto"),
            "user_agent": request.headers.get("user-agent"),
        }

    @app.get("/big")
    async def big():
        return {"data": "x" * 100_000}

    return app


@pytest.fixture
def settings():
    return SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR="/tmp/td-test",
        TD_SBH_CONTAINER_RUNTIME="runc",
    )


def _seed_record(registry, task_id: str, port: int) -> None:
    """Seed a record where the proxy talks to 127.0.0.1:<port>.

    The proxy uses `record.container_name` as the upstream host. In
    production that's the sandbox container's DNS name on its docker
    network; in tests we just use 127.0.0.1 + the fake-upstream port
    via internal_port."""
    now = datetime.now(UTC)
    asyncio.run(registry.add(SandboxRecord(
        task_id=task_id,
        container_id="cid",
        container_name="127.0.0.1",  # tests run upstream on loopback
        network_name=f"td-sandbox-net-{task_id}",
        host_port=port,         # legacy field, kept for compatibility
        internal_port=port,     # this is what the proxy reads
        runtime="static",
        image="x",
        base_path=f"/sandbox/{task_id}/",
        started_at=now,
        last_request_at=now,
        status="running",
    )))


def test_proxy_forwards_get_root(settings):
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-root", port)
        with TestClient(app) as client:
            r = client.get("/sandbox/t-root/")
        assert r.status_code == 200
        assert r.json()["path"] == "/"


def test_proxy_forwards_path_and_query(settings):
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-q", port)
        with TestClient(app) as client:
            r = client.get("/sandbox/t-q/foo/bar?x=1&y=hello")
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == "/foo/bar"
        assert body["query"] == {"x": "1", "y": "hello"}


def test_proxy_forwards_post_body(settings):
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-post", port)
        with TestClient(app) as client:
            r = client.post(
                "/sandbox/t-post/echo",
                json={"hello": "world"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["method"] == "POST"
        assert "hello" in body["body"]
        assert body["ct"] == "application/json"


def test_proxy_sets_x_forwarded_headers(settings):
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-fwd", port)
        with TestClient(app) as client:
            r = client.get(
                "/sandbox/t-fwd/headers",
                headers={"User-Agent": "smoke-test/1.0"},
            )
        body = r.json()
        # X-Forwarded-Host should be set.
        assert body["x_forwarded_host"]
        assert body["user_agent"] == "smoke-test/1.0"


def test_proxy_streams_large_response(settings):
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-big", port)
        with TestClient(app) as client:
            r = client.get("/sandbox/t-big/big")
        assert r.status_code == 200
        assert len(r.json()["data"]) == 100_000


def test_proxy_websocket_unknown_task_closes(settings):
    """Unknown task: ws is rejected with a close frame, not a 404.

    This is the only WS unit test we keep — it doesn't require a live
    upstream socket. Full bidirectional pumping is covered by the
    end-to-end harness on Tokyo (see runbook §18) because the local
    Python 3.14 + uvicorn 0.46 + websockets 16 combo has a known
    handshake-403 issue that doesn't reproduce on the production
    Python 3.12 image.
    """
    from starlette.websockets import WebSocketDisconnect
    app = create_app(settings)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/sandbox/no-such-task/ws/echo") as ws:
                ws.receive_text()  # must trigger the close


def test_proxy_returns_404_for_unknown_task(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/sandbox/no-such-task/anything")
    assert r.status_code == 404


def test_proxy_rewrites_absolute_paths_in_html(settings):
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-html", port)
        with TestClient(app) as client:
            r = client.get("/sandbox/t-html/page.html")
        assert r.status_code == 200
        body = r.text
        # Absolute attrs are rewritten with the prefix.
        assert 'href="/sandbox/t-html/style.css"' in body
        assert 'src="/sandbox/t-html/app.js"' in body
        assert 'href="/sandbox/t-html/about"' in body
        assert 'action="/sandbox/t-html/submit"' in body
        # Protocol-relative left alone.
        assert 'href="//cdn.example.com/x.js"' in body
        # Full URLs left alone.
        assert 'href="https://example.com/x"' in body
        # Relative paths left alone.
        assert 'href="./relative.html"' in body
        # Inline JS string is NOT touched (regression guard against
        # broad regex — see proxy.py docstring).
        assert 'fetch("/api/x")' in body


def test_proxy_does_not_rewrite_json_responses(settings):
    """Bug-fix-discipline regression guard: a value of "/foo" inside
    JSON must survive untouched even if a key is named "href"."""
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-json", port)
        with TestClient(app) as client:
            r = client.get("/sandbox/t-json/json-with-paths")
        assert r.status_code == 200
        # Verbatim JSON: rewriter must not have run on application/json.
        data = r.json()
        assert data["href"] == "/should-stay"
        assert data["data"] == "/x/y"


def test_proxy_does_not_rewrite_text_plain(settings):
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-plain", port)
        with TestClient(app) as client:
            r = client.get("/sandbox/t-plain/text-plain")
        assert r.status_code == 200
        # text/plain skips rewriting — body is byte-identical to upstream.
        assert r.text == '<a href="/keep-me">x</a>'


def test_proxy_bumps_last_request_at(settings):
    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-touch", port)
        rec = asyncio.run(app.state.registry.get("t-touch"))
        old = rec.last_request_at
        time.sleep(0.05)

        with TestClient(app) as client:
            client.get("/sandbox/t-touch/")

        rec2 = asyncio.run(app.state.registry.get("t-touch"))
        assert rec2.last_request_at > old


# ---- P-H Phase 7: upstream probe -----------------------------------


def _free_dead_port() -> int:
    """Return a port number that's almost certainly closed.
    socket.bind+close gives us one that the OS just released; nothing
    is listening there in the brief window before another process
    might reuse it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_proxy_502s_when_upstream_is_dead_and_marks_stopped(settings):
    """If the registry says the sandbox is running but the TCP probe
    can't reach it, return 502 quickly and update the DB row so the
    UI / reconciler see the right state. (P-H Phase 7)"""
    from sandbox_host.proxy import _PROBE_CACHE

    app = create_app(settings)
    dead_port = _free_dead_port()
    # Seed a record pointing at a port nothing is listening on.
    _seed_record(app.state.registry, "t-dead", dead_port)
    # Clear any cache from earlier test runs in the same process.
    _PROBE_CACHE.pop("t-dead", None)

    with TestClient(app) as client:
        r = client.get("/sandbox/t-dead/anything")

    assert r.status_code == 502
    assert "upstream unreachable" in r.text.lower()

    # DB row was flipped to stopped.
    rec = asyncio.run(app.state.registry.get("t-dead"))
    assert rec is not None
    assert rec.status == "stopped"
    assert rec.error_message and "upstream unreachable" in rec.error_message.lower()


def test_proxy_probe_cached_so_repeated_requests_dont_reconnect(settings):
    """A successful probe should be cached for ~10s so the hot
    path doesn't pay a TCP connect cost per request."""
    from sandbox_host.proxy import _PROBE_CACHE

    app = create_app(settings)
    with _running_upstream(_fake_upstream()) as port:
        _seed_record(app.state.registry, "t-cache", port)
        _PROBE_CACHE.pop("t-cache", None)
        with TestClient(app) as client:
            # First request populates cache.
            r1 = client.get("/sandbox/t-cache/")
            assert r1.status_code == 200
            cached = _PROBE_CACHE.get("t-cache")
            assert cached is not None
            assert cached[0] is True  # alive
            ttl_after_first = cached[1]

            # Second request reuses cache — TTL should not move forward
            # (it gets refreshed only when we re-probe).
            r2 = client.get("/sandbox/t-cache/")
            assert r2.status_code == 200
            cached2 = _PROBE_CACHE.get("t-cache")
            assert cached2 is not None
            assert cached2[1] == ttl_after_first
