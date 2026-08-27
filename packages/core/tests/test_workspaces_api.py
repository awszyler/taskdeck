from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from taskdeck_core.main import create_app


def test_create_and_list_workspaces():
    app = create_app()
    with TestClient(app) as client:
        # Create
        slug = f"t-{uuid4().hex[:8]}"
        r = client.post(
            "/api/v1/workspaces",
            json={"slug": slug, "name": "T"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == slug
        assert body["name"] == "T"
        assert "id" in body
        assert "created_at" in body

        # List — include at least our new one
        r = client.get("/api/v1/workspaces")
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(w["slug"] == slug for w in items)


def test_duplicate_slug_returns_409():
    app = create_app()
    with TestClient(app) as client:
        slug = f"dup-{uuid4().hex[:8]}"
        r = client.post("/api/v1/workspaces", json={"slug": slug, "name": "A"})
        assert r.status_code == 201

        r = client.post("/api/v1/workspaces", json={"slug": slug, "name": "B"})
        assert r.status_code == 409


def test_invalid_slug_rejected():
    app = create_app()
    with TestClient(app) as client:
        r = client.post("/api/v1/workspaces", json={"slug": "BAD slug!", "name": "X"})
        assert r.status_code == 422
