from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from taskdeck_core.main import create_app


def _create_ws(client, slug):
    r = client.post("/api/v1/workspaces", json={"slug": slug, "name": slug})
    assert r.status_code == 201, r.text
    return r.json()


def _create_task(client, ws_id, **overrides):
    body = {
        "workspace_id": ws_id,
        "title": "t",
        "prompt": "echo",
        "origin": "web",
        "agent": "shell",
    }
    body.update(overrides)
    return client.post("/api/v1/tasks", json=body)


def test_create_task_with_valid_deps():
    app = create_app()
    with TestClient(app) as client:
        ws = _create_ws(client, f"dep-ok-{uuid4().hex[:6]}")
        parent = _create_task(client, ws["id"]).json()
        r = _create_task(client, ws["id"], depends_on=[parent["id"]])
    assert r.status_code == 201
    body = r.json()
    assert body["dependencies_count"] == 1


def test_create_child_while_parent_not_done_goes_blocked():
    app = create_app()
    with TestClient(app) as client:
        ws = _create_ws(client, f"blk-{uuid4().hex[:6]}")
        parent = _create_task(client, ws["id"]).json()
        # Parent is pending (not done). A structured child with this dep
        # is queued straight to BLOCKED at create time — no DRAFT, no
        # separate submit step.
        child = _create_task(client, ws["id"], depends_on=[parent["id"]]).json()
    assert child["status"] == "blocked"


def test_create_with_dep_in_different_workspace_rejects():
    app = create_app()
    with TestClient(app) as client:
        ws1 = _create_ws(client, f"ws1-{uuid4().hex[:6]}")
        ws2 = _create_ws(client, f"ws2-{uuid4().hex[:6]}")
        parent_in_ws1 = _create_task(client, ws1["id"]).json()
        r = _create_task(client, ws2["id"], depends_on=[parent_in_ws1["id"]])
    assert r.status_code == 400


def test_too_many_deps_rejected():
    app = create_app()
    with TestClient(app) as client:
        ws = _create_ws(client, f"many-{uuid4().hex[:6]}")
        # Seed 21 parents, try to use all
        parents = [_create_task(client, ws["id"]).json()["id"] for _ in range(21)]
        r = _create_task(client, ws["id"], depends_on=parents)
    # pydantic rejects on max_length
    assert r.status_code == 422


def test_get_dependencies_lists_parents():
    app = create_app()
    with TestClient(app) as client:
        ws = _create_ws(client, f"list-{uuid4().hex[:6]}")
        p1 = _create_task(client, ws["id"]).json()
        p2 = _create_task(client, ws["id"]).json()
        child = _create_task(client, ws["id"], depends_on=[p1["id"], p2["id"]]).json()
        r = client.get(f"/api/v1/tasks/{child['id']}/dependencies")
    assert r.status_code == 200
    body = r.json()
    assert len(body["parents"]) == 2
    parent_ids = {p["id"] for p in body["parents"]}
    assert parent_ids == {p1["id"], p2["id"]}
