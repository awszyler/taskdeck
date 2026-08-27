from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect
from starlette.websockets import WebSocketDisconnect as StarletteWebSocketDisconnect
from taskdeck_core.main import create_app


def test_rejects_missing_auth():
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    with pytest.raises((WebSocketDisconnect, StarletteWebSocketDisconnect, Exception)), client.websocket_connect("/api/v1/crp/connect"):
        pass


def test_hello_welcome_handshake(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TD_RUNNER_BEARER_TOKEN", "secret")
    app = create_app()
    client = TestClient(app)
    with client:  # noqa: SIM117 — outer context starts lifespan; inner is WS
        with client.websocket_connect(
            "/api/v1/crp/connect",
            headers={"Authorization": "Bearer secret"},
        ) as ws:
            ws.send_json(
                {
                    "type": "hello",
                    "runner_id": "r-1",
                    "capabilities": ["shell"],
                    "max_parallel": 2,
                    "isolation_modes": ["worktree"],
                    "version": "0.0.1",
                }
            )
            msg = ws.receive_json()
            assert msg["type"] == "welcome"
            assert msg["heartbeat_interval"] >= 1
