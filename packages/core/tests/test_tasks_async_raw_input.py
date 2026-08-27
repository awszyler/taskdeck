"""Tests for the async raw-input path on POST /api/v1/tasks.

The async path differs from the structured form in three ways the tests cover:
- Server creates the task in PARSING state with agent="" and a placeholder title.
- A background asyncio.Task runs the IntentParseLoop and transitions the task.
- Idempotency: same (workspace_id, idempotency_key) → same task on retry.

We don't await the background loop in these tests — they verify the
synchronous response shape and idempotency. End-to-end loop behavior is
covered by test_intent_parse_loop.py + the live acceptance scenarios.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import Workspace
from taskdeck_core.intent.parser import IntentParser
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


class _StaticParser(IntentParser):
    """IntentParser stand-in that the async runner reads from app.state.intent_parser.

    The async path constructs an IntentParseLoop from parser._client + parser._model,
    so we need those attrs set."""

    def __init__(self):
        self._client = _NeverCalledClient()
        self._model = "test-model"
        self._timeout = 1.0
        self.last_usage = None
        self.last_model = None


class _NeverCalledClient:
    async def create_completion(self, **_):
        raise AssertionError("LLM should not be called in this test")


async def _setup_app():
    sm = await get_sessionmaker_for_tests()
    app = create_app()
    app.state.db_sessionmaker = sm
    # ASGITransport doesn't run lifespan; pre-seed state.
    app.state.settings = Settings()  # type: ignore[call-arg]
    app.state.intent_parser = _StaticParser()
    app.state.event_bus = None
    app.state.runner_hub = None
    app.state.dispatcher = None

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    slug = f"test-{uuid.uuid4().hex[:8]}"
    async with sm() as s:
        ws = Workspace(slug=slug, name=slug)
        s.add(ws)
        await s.commit()
        ws_id = str(ws.id)
    return app, ws_id


@pytest.mark.asyncio
async def test_raw_input_creates_parsing_task():
    app, ws_id = await _setup_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id,
            "raw_input": "在 https://github.com/awszyler 下新建测试用的贪吃蛇项目",
            "idempotency_key": str(uuid.uuid4()),
            "origin": "web",
        })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "parsing"
    assert body["agent"] == ""  # filled by background loop
    # Title is a placeholder until the loop overwrites it.
    assert "贪吃蛇" in body["title"]


@pytest.mark.asyncio
async def test_raw_input_and_prompt_are_mutually_exclusive():
    app, ws_id = await _setup_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id,
            "raw_input": "x",
            "prompt": "y",
            "agent": "shell",
            "title": "z",
            "origin": "web",
        })
    assert r.status_code == 400
    assert "mutually exclusive" in r.json()["detail"]


@pytest.mark.asyncio
async def test_neither_raw_input_nor_prompt_is_rejected():
    app, ws_id = await _setup_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id,
            "origin": "web",
        })
    assert r.status_code == 400
    assert "either prompt or raw_input" in r.json()["detail"]


@pytest.mark.asyncio
async def test_idempotency_returns_same_task_on_repeat():
    """Two POSTs with the same (workspace_id, idempotency_key) return the same id."""
    app, ws_id = await _setup_app()
    key = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r1 = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id,
            "raw_input": "do something",
            "idempotency_key": key,
            "origin": "web",
        })
        assert r1.status_code == 201, r1.text
        first_id = r1.json()["id"]

        r2 = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id,
            "raw_input": "do something",
            "idempotency_key": key,
            "origin": "web",
        })
        assert r2.status_code in (200, 201), r2.text
        assert r2.json()["id"] == first_id, "second POST must return the same task id"


@pytest.mark.asyncio
async def test_idempotency_different_workspaces_with_same_key_are_distinct():
    app, ws_id_a = await _setup_app()
    sm = app.state.db_sessionmaker
    async with sm() as s:
        slug = f"test-{uuid.uuid4().hex[:8]}"
        ws_b = Workspace(slug=slug, name=slug)
        s.add(ws_b)
        await s.commit()
        ws_id_b = str(ws_b.id)

    key = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r1 = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id_a, "raw_input": "x", "idempotency_key": key, "origin": "web",
        })
        r2 = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id_b, "raw_input": "x", "idempotency_key": key, "origin": "web",
        })
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


@pytest.mark.asyncio
async def test_structured_path_still_works_with_idempotency_key():
    """Old structured-form callers can opt into idempotency too — same key
    returns the same task on retry."""
    app, ws_id = await _setup_app()
    key = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r1 = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id,
            "title": "echo hi",
            "prompt": "echo hi",
            "agent": "shell",
            "origin": "web",
            "idempotency_key": key,
        })
        assert r1.status_code == 201
        first_id = r1.json()["id"]
        r2 = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id,
            "title": "echo hi",
            "prompt": "echo hi",
            "agent": "shell",
            "origin": "web",
            "idempotency_key": key,
        })
        assert r2.json()["id"] == first_id


@pytest.mark.asyncio
async def test_legacy_calls_without_idempotency_key_unaffected():
    """No key → behaves exactly like before, every call creates a new task."""
    app, ws_id = await _setup_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r1 = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id, "title": "a", "prompt": "echo a", "agent": "shell", "origin": "web",
        })
        r2 = await ac.post("/api/v1/tasks", json={
            "workspace_id": ws_id, "title": "b", "prompt": "echo b", "agent": "shell", "origin": "web",
        })
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
