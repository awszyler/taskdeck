from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import Workspace
from taskdeck_core.main import create_app


@pytest.mark.asyncio
async def test_create_task_then_submit_then_list_then_cancel():
    # Bootstrap a workspace row directly — M1 has no workspace API.
    slug = f"test-{uuid.uuid4().hex[:8]}"
    sm = await get_sessionmaker_for_tests()
    async with sm() as sess:
        ws = Workspace(slug=slug, name=slug)
        sess.add(ws)
        await sess.commit()
        ws_id = str(ws.id)

    app = create_app()

    # Override the get_session dependency to use our test sessionmaker.
    # Also set db_sessionmaker on app state for the submit_task dispatch path.
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/tasks",
            json={
                "workspace_id": ws_id,
                "title": "echo hi",
                "prompt": "echo hi",
                "origin": "web",
                "agent": "shell",
            },
        )
        assert r.status_code == 201, r.text
        task = r.json()
        # Structured-form create now queues directly — no DRAFT step.
        assert task["status"] == "pending"
        tid = task["id"]

        r = await ac.get("/api/v1/tasks", params={"status": "pending"})
        assert r.status_code == 200
        body = r.json()
        assert any(t["id"] == tid for t in body["items"])

        r = await ac.post(f"/api/v1/tasks/{tid}/cancel")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
