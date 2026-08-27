from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import ImIdentityLink, Task, User, Workspace
from taskdeck_core.im.wecom.notifier import WecomNotifier


class _FakeClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, *, to_user: str, content: str) -> None:
        self.sent.append((to_user, content))

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_notifies_only_on_terminal_states():
    sm = await get_sessionmaker_for_tests()
    ext_id = f"UserZ-{uuid4().hex[:6]}"
    async with sm() as s:
        ws = Workspace(slug=f"n-{uuid4().hex[:6]}", name="n")
        s.add(ws)
        await s.commit()
        user = User(workspace_id=ws.id, email="u@t", name="u", role="member")
        s.add(user)
        await s.commit()
        link = ImIdentityLink(
            workspace_id=ws.id,
            user_id=user.id,
            platform="wecom",
            external_id=ext_id,
            created_at=datetime.now(UTC),
        )
        s.add(link)
        await s.commit()
        task = Task(
            workspace_id=ws.id,
            title="t",
            prompt="p",
            origin="im",
            agent="shell",
            status="running",
            created_by=user.id,
        )
        s.add(task)
        await s.commit()
        task_id = str(task.id)

    fake = _FakeClient()
    notifier = WecomNotifier(client=fake, sessionmaker=sm, public_base_url="http://h")

    # Non-terminal: no send
    await notifier.handle({"type": "task.event", "task_id": task_id, "to": "running"})
    assert fake.sent == []

    # Terminal done: send
    await notifier.handle({"type": "task.event", "task_id": task_id, "to": "done"})
    assert len(fake.sent) == 1
    assert fake.sent[0][0] == ext_id
    assert "done" in fake.sent[0][1]

    # Terminal failed: also sends
    await notifier.handle({"type": "task.event", "task_id": task_id, "to": "failed"})
    assert len(fake.sent) == 2


@pytest.mark.asyncio
async def test_ignores_non_im_tasks():
    sm = await get_sessionmaker_for_tests()
    ext_id = f"UserY-{uuid4().hex[:6]}"
    async with sm() as s:
        ws = Workspace(slug=f"n2-{uuid4().hex[:6]}", name="n2")
        s.add(ws)
        await s.commit()
        user = User(workspace_id=ws.id, email="u@t", name="u", role="member")
        s.add(user)
        await s.commit()
        link = ImIdentityLink(
            workspace_id=ws.id,
            user_id=user.id,
            platform="wecom",
            external_id=ext_id,
            created_at=datetime.now(UTC),
        )
        s.add(link)
        await s.commit()
        task = Task(
            workspace_id=ws.id,
            title="t",
            prompt="p",
            origin="web",
            agent="shell",
            status="running",
            created_by=user.id,
        )
        s.add(task)
        await s.commit()
        task_id = str(task.id)

    fake = _FakeClient()
    notifier = WecomNotifier(client=fake, sessionmaker=sm, public_base_url="http://h")
    await notifier.handle({"type": "task.event", "task_id": task_id, "to": "done"})
    assert fake.sent == []


@pytest.mark.asyncio
async def test_ignores_when_no_link():
    sm = await get_sessionmaker_for_tests()
    async with sm() as s:
        ws = Workspace(slug=f"n3-{uuid4().hex[:6]}", name="n3")
        s.add(ws)
        await s.commit()
        user = User(workspace_id=ws.id, email="u@t", name="u", role="member")
        s.add(user)
        await s.commit()
        task = Task(
            workspace_id=ws.id,
            title="t",
            prompt="p",
            origin="im",
            agent="shell",
            status="running",
            created_by=user.id,
        )
        s.add(task)
        await s.commit()
        task_id = str(task.id)

    fake = _FakeClient()
    notifier = WecomNotifier(client=fake, sessionmaker=sm, public_base_url="http://h")
    await notifier.handle({"type": "task.event", "task_id": task_id, "to": "done"})
    assert fake.sent == []
