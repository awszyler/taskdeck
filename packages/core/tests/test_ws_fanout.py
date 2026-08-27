from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, Workspace
from taskdeck_core.main import create_app


def test_task_event_is_broadcast_to_ui_subscribers():
    app = create_app()
    client = TestClient(app)

    async def seed() -> str:
        """Insert seed data inside the TestClient's anyio event loop."""
        sm = await get_sessionmaker_for_tests()
        slug = f"fanout-{uuid.uuid4().hex[:8]}"
        async with sm() as sess:
            ws = Workspace(slug=slug, name="fanout")
            sess.add(ws)
            await sess.commit()
            task = Task(
                workspace_id=ws.id,
                title="x",
                prompt="x",
                origin="web",
                agent="shell",
                status="parse_failed",
            )
            sess.add(task)
            await sess.commit()
            return str(task.id)

    with client:
        # Seed inside the same anyio event loop that the TestClient uses.
        tid = client.portal.call(seed)

        with client.websocket_connect("/api/v1/ws") as ws:
            r = client.post(f"/api/v1/tasks/{tid}/submit")
            assert r.status_code == 200
            ev = ws.receive_json()
            assert ev["type"] == "task.event"
            assert ev["task_id"] == tid
            assert ev["from"] == "parse_failed"
            assert ev["to"] == "pending"
