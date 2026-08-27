from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from taskdeck_core.main import create_app


def test_issue_bind_code_for_existing_workspace():
    app = create_app()
    with TestClient(app) as client:
        slug = f"bc-{uuid4().hex[:6]}"
        ws = client.post("/api/v1/workspaces", json={"slug": slug, "name": slug}).json()
        r = client.post("/api/v1/im/wecom/bind-code", json={"workspace_id": ws["id"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["code"]) == 6
    assert body["expires_at"] > 0


def test_issue_bind_code_for_missing_workspace():
    app = create_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/im/wecom/bind-code",
            json={"workspace_id": str(uuid4())},
        )
    assert r.status_code == 404


def test_identity_links_list_empty():
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/api/v1/im/identity-links")
    assert r.status_code == 200
    # New suite may have links from other tests — just check shape.
    assert "items" in r.json()
