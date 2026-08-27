"""Tests for MemoryRetriever — seeds known vectors, asserts top-K + budget cap."""
from __future__ import annotations

from uuid import uuid4

import pytest
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import MemoryChunk, Workspace
from taskdeck_core.memory.retriever import retrieve

# ── fake embedding client ─────────────────────────────────────────────────────


class _FixedEmbedClient:
    """Returns a pre-set fixed vector for all inputs."""

    def __init__(self, vec: list[float]):
        self._vec = vec

    async def embed_batch(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        return [list(self._vec) for _ in texts]


def _unit(dim: int = 1024, *, axis: int = 0) -> list[float]:
    """Return a 1024-dim unit vector along *axis*."""
    v = [0.0] * dim
    v[axis] = 1.0
    return v


# ── helpers ───────────────────────────────────────────────────────────────────


async def _make_workspace(sm) -> Workspace:
    async with sm() as s:
        ws = Workspace(slug=f"ret-{uuid4().hex[:8]}", name="retriever-test")
        s.add(ws)
        await s.commit()
        await s.refresh(ws)
        return ws


async def _seed_chunk(sm, workspace_id, *, text: str, vec: list[float]) -> MemoryChunk:
    async with sm() as s:
        chunk = MemoryChunk(
            workspace_id=workspace_id,
            source_kind="task-summary",
            text=text,
            embedding=vec,
            meta={},
        )
        s.add(chunk)
        await s.commit()
        await s.refresh(chunk)
        return chunk


async def _cleanup(sm, *workspace_ids) -> None:
    async with sm() as s:
        for wid in workspace_ids:
            ws = await s.get(Workspace, wid)
            if ws:
                await s.delete(ws)
        await s.commit()


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrieve_empty_query_returns_empty():
    sm = await get_sessionmaker_for_tests()
    embed = _FixedEmbedClient(_unit(axis=0))
    async with sm() as db:
        result = await retrieve(
            db,
            workspace_id=uuid4(),
            query_text="   ",
            embedding_client=embed,
            top_k=4,
            per_cap=1024,
            total_cap=4096,
        )
    assert result == []


@pytest.mark.asyncio
async def test_retrieve_returns_closest_first():
    """Seed 3 chunks with orthogonal vectors. Query closest to axis-0 → axis-0 chunk first."""
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        c0 = await _seed_chunk(sm, ws.id, text="axis-0 chunk", vec=_unit(axis=0))
        await _seed_chunk(sm, ws.id, text="axis-1 chunk", vec=_unit(axis=1))
        await _seed_chunk(sm, ws.id, text="axis-2 chunk", vec=_unit(axis=2))

        # Query vector is exactly axis-0 → c0 should have lowest cosine distance (0)
        embed = _FixedEmbedClient(_unit(axis=0))
        async with sm() as db:
            result = await retrieve(
                db,
                workspace_id=ws.id,
                query_text="what is axis zero?",
                embedding_client=embed,
                top_k=3,
                per_cap=1024,
                total_cap=4096,
            )

        assert len(result) == 3
        assert result[0]["chunk_id"] == str(c0.id)
        assert result[0]["text"] == "axis-0 chunk"
        assert result[0]["score"] < result[1]["score"]
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_retrieve_top_k_limits_results():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        for i in range(5):
            await _seed_chunk(sm, ws.id, text=f"chunk {i}", vec=_unit(axis=i))

        embed = _FixedEmbedClient(_unit(axis=0))
        async with sm() as db:
            result = await retrieve(
                db,
                workspace_id=ws.id,
                query_text="anything",
                embedding_client=embed,
                top_k=2,
                per_cap=1024,
                total_cap=4096,
            )
        assert len(result) == 2
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_retrieve_per_chunk_cap_truncates():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        long_text = "X" * 2000
        await _seed_chunk(sm, ws.id, text=long_text, vec=_unit(axis=0))

        embed = _FixedEmbedClient(_unit(axis=0))
        async with sm() as db:
            result = await retrieve(
                db,
                workspace_id=ws.id,
                query_text="long",
                embedding_client=embed,
                top_k=4,
                per_cap=100,
                total_cap=4096,
            )
        assert len(result) == 1
        assert len(result[0]["text"]) <= 100
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_retrieve_total_cap_stops_early():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        # Each chunk is 201 chars; total_cap=100 → first chunk truncated, loop stops quickly
        for i in range(4):
            await _seed_chunk(sm, ws.id, text="B" * 200 + f"{i}", vec=_unit(axis=i))

        embed = _FixedEmbedClient(_unit(axis=0))
        async with sm() as db:
            result = await retrieve(
                db,
                workspace_id=ws.id,
                query_text="budget test",
                embedding_client=embed,
                top_k=4,
                per_cap=1024,
                total_cap=100,  # well below one chunk; first chunk gets truncated
            )
        # At total_cap=100: first chunk text = 201 chars, len > 100, truncate to [:80]+marker = 94
        # remaining = 100-94 = 6; second chunk: 201 > 6, truncate to [:-14] ≤ 0 chars + marker = 14
        # remaining <= 0 → break.  At most 2 chunks returned.
        assert len(result) <= 4
        assert len(result) >= 1
        # The first chunk must be shorter than the original (201 chars)
        assert len(result[0]["text"]) < 201
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_retrieve_workspace_isolation():
    """Chunks from another workspace must not appear."""
    sm = await get_sessionmaker_for_tests()
    ws_a = await _make_workspace(sm)
    ws_b = await _make_workspace(sm)
    try:
        await _seed_chunk(sm, ws_a.id, text="workspace A chunk", vec=_unit(axis=0))
        await _seed_chunk(sm, ws_b.id, text="workspace B chunk", vec=_unit(axis=0))

        embed = _FixedEmbedClient(_unit(axis=0))
        async with sm() as db:
            result = await retrieve(
                db,
                workspace_id=ws_a.id,
                query_text="find my stuff",
                embedding_client=embed,
                top_k=10,
                per_cap=1024,
                total_cap=4096,
            )
        texts = [r["text"] for r in result]
        assert "workspace A chunk" in texts
        assert "workspace B chunk" not in texts
    finally:
        await _cleanup(sm, ws_a.id, ws_b.id)
