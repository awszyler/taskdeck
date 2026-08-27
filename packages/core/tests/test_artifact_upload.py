from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, Workspace
from taskdeck_core.main import create_app


def test_upload_artifact_requires_auth():
    app = create_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/internal/artifacts",
            content=b"data",
            headers={"X-Task-ID": str(uuid4()), "X-Artifact-Kind": "git-diff"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_upload_artifact_persists_row_and_file(tmp_path, monkeypatch):
    # Point artifact dir at a temp path so the test is hermetic on disk.
    monkeypatch.setenv("TD_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("TD_RUNNER_BEARER_TOKEN", "t-upload")

    app = create_app()
    # Seed workspace + task using a test sessionmaker (separate from app).
    sm = await get_sessionmaker_for_tests()
    async with sm() as sess:
        ws = Workspace(slug=f"au-{uuid4().hex[:6]}", name="au")
        sess.add(ws)
        await sess.commit()
        task = Task(
            workspace_id=ws.id,
            title="x",
            prompt="x",
            origin="web",
            agent="shell",
            status="pending",
        )
        sess.add(task)
        await sess.commit()
        task_id = str(task.id)

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/internal/artifacts",
            content=b"diff content here",
            headers={
                "Authorization": "Bearer t-upload",
                "X-Task-ID": task_id,
                "X-Artifact-Kind": "git-diff",
                "X-Artifact-Meta": json.dumps({"branch": "taskdeck/t-1"}),
            },
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["task_id"] == task_id
    assert body["kind"] == "git-diff"
    assert body["size_bytes"] == len(b"diff content here")

    # File on disk
    path = tmp_path / task_id / "git-diff"
    assert path.exists()
    assert path.read_bytes() == b"diff content here"
