"""Skeleton tests for the sandbox-host FastAPI app.

These don't touch docker — they verify routing, settings loading,
and the empty-registry response shape. Provisioning + proxy tests
come in P6.3.1.4 and P6.3.1.8.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sandbox_host.main import create_app
from sandbox_host.settings import SandboxHostSettings


@pytest.fixture
def app(tmp_path):
    # Use a per-test tmp work_dir so the SQLite-backed registry
    # (P-H Phase 1) starts empty for every run. Previously /tmp/td-test
    # was shared across tests and the state.db there accumulated rows.
    s = SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR=str(tmp_path),
        TD_SBH_CONTAINER_RUNTIME="runc",  # gVisor not installed locally
        TD_SBH_MAX_CONCURRENT=2,
    )
    return create_app(s)


def test_health_returns_ok(app):
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["sandboxes"] == 0
    assert body["max_concurrent"] == 2
    assert body["runtime"] == "runc"


def test_status_empty_registry(app):
    client = TestClient(app)
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["sandboxes"] == []


def test_settings_resolve_from_env_aliases(app):
    s = app.state.settings
    assert s.container_runtime == "runc"
    assert s.cpu_limit == 1.0
    assert s.memory_limit_mb == 1024
    assert s.idle_seconds == 300
    assert s.network_prefix == "td-sandbox-net"
