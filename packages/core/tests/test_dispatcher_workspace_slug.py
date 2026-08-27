"""Regression: dispatcher must include workspace_slug in task.assign payload.

Without it, the runner can't isolate workspace filesystem paths and falls
back to a flat layout shared across all logical workspaces.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, Workspace
from taskdeck_core.dispatcher.service import Dispatcher


class _StubSocket:
    """Captures messages sent through send_json so the test can assert on them."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg):
        self.sent.append(msg)


class _StubConn:
    """Stand-in for crp.hub.RunnerConnection."""

    def __init__(self):
        self.socket = _StubSocket()
        self._inflight = 0

    def increment_inflight(self):
        self._inflight += 1


class _StubHub:
    """Stand-in for crp.hub.RunnerHub.pick_for(agent) — always returns the same conn."""

    def __init__(self, conn):
        self._conn = conn

    def pick_for(self, _agent):
        return self._conn


async def _make_workspace_and_task(sm, slug: str) -> tuple[Workspace, Task]:
    async with sm() as sess:
        ws = Workspace(slug=slug, name=slug)
        sess.add(ws)
        await sess.flush()
        task = Task(
            workspace_id=ws.id,
            title="t",
            prompt="echo hi",
            origin="web",
            agent="shell",
            status="pending",
        )
        sess.add(task)
        await sess.commit()
        await sess.refresh(ws)
        await sess.refresh(task)
        return ws, task


@pytest.mark.asyncio
async def test_dispatch_includes_workspace_slug():
    sm = await get_sessionmaker_for_tests()
    ws, _task = await _make_workspace_and_task(sm, slug=f"alpha-{uuid4().hex[:8]}")

    conn = _StubConn()
    hub = _StubHub(conn)
    dispatcher = Dispatcher(hub=hub)

    async with sm() as sess:
        await dispatcher.try_dispatch_pending(sess)

    # The shared test DB may contain pending tasks from earlier tests; assert
    # on slug presence, not exact counts.
    slugs_seen = {m["payload"]["workspace_slug"] for m in conn.socket.sent
                  if m.get("type") == "task.assign"}
    assert ws.slug in slugs_seen


@pytest.mark.asyncio
async def test_dispatch_uses_correct_slug_per_workspace():
    """Two workspaces; each task carries its own workspace's slug — the
    dispatcher doesn't mix them up."""
    sm = await get_sessionmaker_for_tests()
    ws_a, _ = await _make_workspace_and_task(sm, slug=f"alpha-{uuid4().hex[:8]}")
    ws_b, _ = await _make_workspace_and_task(sm, slug=f"beta-{uuid4().hex[:8]}")

    conn = _StubConn()
    hub = _StubHub(conn)
    dispatcher = Dispatcher(hub=hub)

    async with sm() as sess:
        await dispatcher.try_dispatch_pending(sess)

    slugs_seen = {m["payload"]["workspace_slug"] for m in conn.socket.sent
                  if m.get("type") == "task.assign"}
    assert ws_a.slug in slugs_seen
    assert ws_b.slug in slugs_seen


async def _make_workspace_task_with_turns(sm, slug: str, *, agent: str, turns: list[tuple[str, str]]):
    from datetime import UTC, datetime

    from taskdeck_core.db.models import TaskTurn

    async with sm() as sess:
        ws = Workspace(slug=slug, name=slug)
        sess.add(ws)
        await sess.flush()
        task = Task(
            workspace_id=ws.id, title="t", prompt="original prompt",
            origin="web", agent=agent, status="pending",
        )
        sess.add(task)
        await sess.flush()
        for i, (role, content) in enumerate(turns):
            sess.add(TaskTurn(
                task_id=task.id, seq=i, role=role, content=content,
                created_at=datetime.now(UTC),
            ))
        await sess.commit()
        await sess.refresh(task)
        return ws, task


@pytest.mark.asyncio
async def test_dispatch_includes_prior_turns_when_present():
    sm = await get_sessionmaker_for_tests()
    ws, _task = await _make_workspace_task_with_turns(
        sm, slug=f"alpha-{uuid4().hex[:8]}", agent="claude-code",
        turns=[("agent", "may I read?"), ("user", "yes please")],
    )

    conn = _StubConn()
    hub = _StubHub(conn)
    dispatcher = Dispatcher(hub=hub)

    async with sm() as sess:
        await dispatcher.try_dispatch_pending(sess)

    matching = [m for m in conn.socket.sent
                if m.get("type") == "task.assign"
                and m["payload"]["workspace_slug"] == ws.slug]
    assert matching, "expected at least one assign for this workspace"
    payload = matching[0]["payload"]
    turns = payload["prior_turns"]
    assert len(turns) == 2
    assert turns[0]["role"] == "agent"
    assert turns[0]["content"] == "may I read?"
    assert turns[1]["role"] == "user"
    assert turns[1]["content"] == "yes please"


@pytest.mark.asyncio
async def test_dispatch_prepends_ccpt_ask_instructions_for_claude_code():
    """Claude Code prompts get the ccpt:ask protocol header prepended."""
    sm = await get_sessionmaker_for_tests()
    ws, _task = await _make_workspace_task_with_turns(
        sm, slug=f"alpha-{uuid4().hex[:8]}", agent="claude-code", turns=[],
    )

    conn = _StubConn()
    hub = _StubHub(conn)
    dispatcher = Dispatcher(hub=hub)

    async with sm() as sess:
        await dispatcher.try_dispatch_pending(sess)

    matching = [m for m in conn.socket.sent
                if m.get("type") == "task.assign"
                and m["payload"]["workspace_slug"] == ws.slug]
    assert matching
    prompt = matching[0]["payload"]["prompt"]
    assert "<ccpt:ask>" in prompt
    assert "original prompt" in prompt
    assert prompt.index("<ccpt:ask>") < prompt.index("original prompt")


@pytest.mark.asyncio
async def test_dispatch_does_not_prepend_for_shell():
    """Shell agent doesn't get the ccpt:ask header (shell can't ask)."""
    sm = await get_sessionmaker_for_tests()
    ws, _task = await _make_workspace_task_with_turns(
        sm, slug=f"alpha-{uuid4().hex[:8]}", agent="shell", turns=[],
    )

    conn = _StubConn()
    hub = _StubHub(conn)
    dispatcher = Dispatcher(hub=hub)

    async with sm() as sess:
        await dispatcher.try_dispatch_pending(sess)

    matching = [m for m in conn.socket.sent
                if m.get("type") == "task.assign"
                and m["payload"]["workspace_slug"] == ws.slug]
    assert matching
    prompt = matching[0]["payload"]["prompt"]
    assert "<ccpt:ask>" not in prompt
    assert prompt == "original prompt"


@pytest.mark.asyncio
async def test_dispatch_caps_prior_turns_at_byte_limit():
    """Many large turns get capped; placeholder for dropped turns appears."""
    long = "x" * 5000  # 5KB per turn
    turns = [("agent" if i % 2 == 0 else "user", long) for i in range(20)]  # ~100KB total
    sm = await get_sessionmaker_for_tests()
    ws, _task = await _make_workspace_task_with_turns(
        sm, slug=f"alpha-{uuid4().hex[:8]}", agent="claude-code", turns=turns,
    )

    conn = _StubConn()
    hub = _StubHub(conn)
    dispatcher = Dispatcher(hub=hub)

    async with sm() as sess:
        await dispatcher.try_dispatch_pending(sess)

    matching = [m for m in conn.socket.sent
                if m.get("type") == "task.assign"
                and m["payload"]["workspace_slug"] == ws.slug]
    assert matching
    prior = matching[0]["payload"]["prior_turns"]

    # Total content bytes (excluding placeholder) must fit in 32KB; placeholder present.
    real_bytes = sum(len(t["content"].encode("utf-8")) for t in prior if "earlier turns omitted" not in t["content"])
    assert real_bytes <= 32 * 1024
    assert any("earlier turns omitted" in t["content"] for t in prior)
