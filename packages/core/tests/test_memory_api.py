"""Tests for GET/POST/DELETE /api/v1/memory."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.db.models import User, Workspace, WorkspaceMember
from taskdeck_core.main import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(sm) -> User:
    async with sm() as sess:
        u = User(
            workspace_id=None,
            email=f"{uuid4().hex[:8]}@test.com",
            name="Memory User",
            role="member",
            cognito_sub=uuid4().hex,
            login=uuid4().hex[:8],
        )
        sess.add(u)
        await sess.commit()
        await sess.refresh(u)
        return u


async def _make_workspace_with_member(sm, user: User) -> Workspace:
    async with sm() as sess:
        ws = Workspace(slug=f"mem-api-{uuid4().hex[:8]}", name="memory-api-test")
        sess.add(ws)
        await sess.flush()
        sess.add(
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=user.id,
                role="owner",
                created_at=datetime.now(UTC),
            )
        )
        await sess.commit()
        await sess.refresh(ws)
        return ws


def _make_app(sm):
    app = create_app()
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return app


class _FakeEmbeddingClient:
    """Returns deterministic 1024-dim zero vectors."""

    async def embed_batch(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        return [[0.0] * 1024 for _ in texts]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_list_chunk():
    """POST a chunk then GET it back in the list."""
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, user)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: user
    app.state.embedding_client = _FakeEmbeddingClient()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        post_r = await ac.post(
            "/api/v1/memory",
            json={"workspace_id": str(ws.id), "text": "always use async generators"},
        )
        assert post_r.status_code == 201, post_r.text
        chunk = post_r.json()
        assert chunk["text"] == "always use async generators"
        assert chunk["source_kind"] == "manual"

        list_r = await ac.get(f"/api/v1/memory?workspace_id={ws.id}")
        assert list_r.status_code == 200, list_r.text
        items = list_r.json()["items"]
        ids = [i["id"] for i in items]
        assert chunk["id"] in ids


@pytest.mark.asyncio
async def test_delete_chunk():
    """DELETE removes the chunk; subsequent list no longer contains it."""
    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, user)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: user
    app.state.embedding_client = _FakeEmbeddingClient()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        post_r = await ac.post(
            "/api/v1/memory",
            json={"workspace_id": str(ws.id), "text": "to be deleted"},
        )
        assert post_r.status_code == 201, post_r.text
        chunk_id = post_r.json()["id"]

        del_r = await ac.delete(f"/api/v1/memory/{chunk_id}")
        assert del_r.status_code == 204, del_r.text

        list_r = await ac.get(f"/api/v1/memory?workspace_id={ws.id}")
        assert list_r.status_code == 200, list_r.text
        ids = [i["id"] for i in list_r.json()["items"]]
        assert chunk_id not in ids


@pytest.mark.asyncio
async def test_workspace_not_visible_returns_404():
    """A user who is not a member of the workspace gets 404 on list and post."""
    sm = await get_sessionmaker_for_tests()
    owner = await _make_user(sm)
    outsider = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, owner)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: outsider
    app.state.embedding_client = _FakeEmbeddingClient()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        list_r = await ac.get(f"/api/v1/memory?workspace_id={ws.id}")
        assert list_r.status_code == 404, list_r.text

        post_r = await ac.post(
            "/api/v1/memory",
            json={"workspace_id": str(ws.id), "text": "secret"},
        )
        assert post_r.status_code == 404, post_r.text


@pytest.mark.asyncio
async def test_list_with_q_no_embedding_client_returns_503():
    """GET with q= when embedding_client is missing (or _MissingEmbeddingClient) → 503."""
    from taskdeck_core.memory.embedding import _MissingEmbeddingClient

    sm = await get_sessionmaker_for_tests()
    user = await _make_user(sm)
    ws = await _make_workspace_with_member(sm, user)

    app = _make_app(sm)
    app.dependency_overrides[current_principal] = lambda: user
    app.state.embedding_client = _MissingEmbeddingClient()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(f"/api/v1/memory?workspace_id={ws.id}&q=something")
        assert r.status_code == 503, r.text
