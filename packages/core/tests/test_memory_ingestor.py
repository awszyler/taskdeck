"""Tests for MemoryIngestor — uses fake artifact store + fake embedding client."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import MemoryChunk, Task, TaskArtifact, Workspace
from taskdeck_core.memory.ingestor import MemoryIngestor, _split_into_hunks

# ── fake collaborators ────────────────────────────────────────────────────────


class _FakeEmbedClient:
    """Returns deterministic 1024-dim unit vectors indexed by call order."""

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed_batch(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(i + 1) / 100.0] * 1024 for i in range(len(texts))]


class _FakeArtifactStore:
    def __init__(self, blobs: dict[str, bytes]):
        self._blobs = blobs

    async def read(self, key: str) -> bytes:
        if key not in self._blobs:
            raise FileNotFoundError(key)
        return self._blobs[key]


# ── helpers ───────────────────────────────────────────────────────────────────


async def _make_workspace(sm) -> Workspace:
    async with sm() as s:
        ws = Workspace(slug=f"ing-{uuid4().hex[:8]}", name="ingestor-test")
        s.add(ws)
        await s.commit()
        await s.refresh(ws)
        return ws


async def _make_task(sm, workspace_id, *, summary: str | None = None) -> Task:
    async with sm() as s:
        task = Task(
            workspace_id=workspace_id,
            title="test task",
            prompt="do the thing",
            origin="web",
            agent="claude-code",
            status="done",
            summary=summary,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return task


async def _make_artifact(sm, task_id, *, kind: str, ref: str) -> TaskArtifact:
    from datetime import UTC, datetime
    async with sm() as s:
        art = TaskArtifact(
            task_id=task_id,
            kind=kind,
            ref=ref,
            meta={},
            created_at=datetime.now(UTC),
        )
        s.add(art)
        await s.commit()
        await s.refresh(art)
        return art


async def _cleanup(sm, *workspace_ids) -> None:
    """Delete workspaces; cascade handles memory_chunks; tasks need explicit delete."""
    from sqlalchemy import delete as sa_delete
    from taskdeck_core.db.models import Task, TaskArtifact
    async with sm() as s:
        for wid in workspace_ids:
            # Delete task artifacts then tasks before workspace (FK constraints)
            tasks = (
                await s.scalars(select(Task).where(Task.workspace_id == wid))
            ).all()
            for t in tasks:
                await s.execute(
                    sa_delete(TaskArtifact).where(TaskArtifact.task_id == t.id)
                )
                await s.delete(t)
            ws = await s.get(Workspace, wid)
            if ws:
                await s.delete(ws)
        await s.commit()


def _make_ingestor(sm, art_store, embed_client, *, enabled=True) -> MemoryIngestor:
    return MemoryIngestor(
        sessionmaker=sm,
        artifact_store=art_store,
        embedding_client=embed_client,
        enabled=enabled,
    )


# ── unit tests — _split_into_hunks ──────────────────────────────────────────


def test_split_short_diff_returns_one_chunk():
    diff = "diff --git a/f b/f\n+line\n"
    chunks = _split_into_hunks(diff)
    assert len(chunks) == 1
    assert "diff --git" in chunks[0]


def test_split_two_file_diffs():
    diff = (
        "diff --git a/foo b/foo\n+foo line\n"
        "diff --git a/bar b/bar\n+bar line\n"
    )
    chunks = _split_into_hunks(diff)
    assert len(chunks) == 2


def test_split_large_file_diff_subchunks():
    # Build a single-file diff that exceeds 2048 bytes by repeating paragraphs.
    para = "A" * 200 + "\n\n"
    big_diff = "diff --git a/f b/f\n" + para * 20  # ~4 KB
    chunks = _split_into_hunks(big_diff, max_bytes=512)
    assert len(chunks) > 1
    assert all(len(c) > 0 for c in chunks)


# ── integration tests — ingestor.handle ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestor_disabled_is_noop():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        task = await _make_task(sm, ws.id, summary="should not ingest")
        embed = _FakeEmbedClient()
        art_store = _FakeArtifactStore({})
        ingestor = _make_ingestor(sm, art_store, embed, enabled=False)
        await ingestor.handle(
            {"type": "task.event", "to": "done", "task_id": str(task.id)}
        )
        async with sm() as s:
            rows = (
                await s.execute(
                    select(MemoryChunk).where(MemoryChunk.workspace_id == ws.id)
                )
            ).scalars().all()
        assert rows == []
        assert embed.calls == []
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_ingestor_wrong_event_type_noop():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        task = await _make_task(sm, ws.id, summary="nope")
        embed = _FakeEmbedClient()
        art_store = _FakeArtifactStore({})
        ingestor = _make_ingestor(sm, art_store, embed)
        # wrong event type
        await ingestor.handle({"type": "task.updated", "task_id": str(task.id)})
        # wrong to status
        await ingestor.handle(
            {"type": "task.event", "to": "running", "task_id": str(task.id)}
        )
        async with sm() as s:
            rows = (
                await s.execute(
                    select(MemoryChunk).where(MemoryChunk.workspace_id == ws.id)
                )
            ).scalars().all()
        assert rows == []
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_ingestor_summary_creates_chunk():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        task = await _make_task(sm, ws.id, summary="We refactored the login form.")
        embed = _FakeEmbedClient()
        art_store = _FakeArtifactStore({})
        ingestor = _make_ingestor(sm, art_store, embed)
        await ingestor.handle(
            {"type": "task.event", "to": "done", "task_id": str(task.id)}
        )
        async with sm() as s:
            rows = (
                await s.execute(
                    select(MemoryChunk).where(MemoryChunk.workspace_id == ws.id)
                )
            ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source_kind == "task-summary"
        assert row.text == "We refactored the login form."
        assert row.source_task_id == task.id
        assert row.embedding is not None
        assert len(row.embedding) == 1024
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_ingestor_decision_artifact_creates_chunk():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        task = await _make_task(sm, ws.id)
        art = await _make_artifact(
            sm, task.id, kind="decision", ref=f"task/{task.id}/decision"
        )
        art_store = _FakeArtifactStore(
            {str(art.ref): b"Use postgres for storage, not SQLite."}
        )
        embed = _FakeEmbedClient()
        ingestor = _make_ingestor(sm, art_store, embed)
        await ingestor.handle(
            {"type": "task.event", "to": "done", "task_id": str(task.id)}
        )
        async with sm() as s:
            rows = (
                await s.execute(
                    select(MemoryChunk).where(MemoryChunk.workspace_id == ws.id)
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].source_kind == "artifact-decision"
        assert "postgres" in rows[0].text
        assert rows[0].source_artifact_id == art.id
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_ingestor_git_diff_creates_chunks():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        task = await _make_task(sm, ws.id)
        diff_text = (
            "diff --git a/foo.py b/foo.py\n+added foo\n"
            "diff --git a/bar.py b/bar.py\n+added bar\n"
        )
        art = await _make_artifact(
            sm, task.id, kind="git-diff", ref=f"task/{task.id}/git-diff"
        )
        art_store = _FakeArtifactStore({str(art.ref): diff_text.encode()})
        embed = _FakeEmbedClient()
        ingestor = _make_ingestor(sm, art_store, embed)
        await ingestor.handle(
            {"type": "task.event", "to": "done", "task_id": str(task.id)}
        )
        async with sm() as s:
            rows = (
                await s.execute(
                    select(MemoryChunk)
                    .where(MemoryChunk.workspace_id == ws.id)
                    .order_by(MemoryChunk.created_at)
                )
            ).scalars().all()
        # Two file diffs → two chunks
        assert len(rows) == 2
        assert all(r.source_kind == "artifact-git-diff" for r in rows)
        texts = {r.text for r in rows}
        assert any("foo.py" in t for t in texts)
        assert any("bar.py" in t for t in texts)
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_ingestor_no_content_no_chunks():
    """Task with no summary and no artifacts → nothing ingested."""
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        task = await _make_task(sm, ws.id, summary=None)
        embed = _FakeEmbedClient()
        art_store = _FakeArtifactStore({})
        ingestor = _make_ingestor(sm, art_store, embed)
        await ingestor.handle(
            {"type": "task.event", "to": "done", "task_id": str(task.id)}
        )
        async with sm() as s:
            rows = (
                await s.execute(
                    select(MemoryChunk).where(MemoryChunk.workspace_id == ws.id)
                )
            ).scalars().all()
        assert rows == []
        assert embed.calls == []
    finally:
        await _cleanup(sm, ws.id)
