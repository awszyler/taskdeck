"""Tests for taskdeck_core.deps.injector.collect_dependency_outputs.

Uses a real DB (test transaction pattern from conftest-like helpers) plus
a stub ArtifactStore so we don't need actual files on disk.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import Task, TaskArtifact, TaskDependency, Workspace
from taskdeck_core.deps.injector import (
    ARTIFACT_KINDS_INJECTED,
    PER_ARTIFACT_CAP,
    TOTAL_CAP,
    collect_dependency_outputs,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


class _FakeStore:
    """In-memory artifact store. Maps ref -> bytes."""

    def __init__(self, data: dict[str, bytes]):
        self._data = data

    async def read(self, key: str) -> bytes:
        if key not in self._data:
            raise FileNotFoundError(f"no artifact: {key}")
        return self._data[key]


async def _make_workspace(session, slug: str) -> Workspace:
    ws = Workspace(slug=slug, name=slug)
    session.add(ws)
    await session.flush()
    return ws


async def _make_task(session, ws_id, *, status: str = "done", title: str = "parent") -> Task:
    t = Task(
        workspace_id=ws_id,
        title=title,
        prompt="x",
        origin="web",
        agent="claude-code",
        status=status,
        finished_at=datetime.now(UTC) if status in {"done", "failed", "cancelled"} else None,
    )
    session.add(t)
    await session.flush()
    return t


async def _add_artifact(
    session,
    task_id,
    *,
    kind: str = "git-diff",
    ref: str | None = None,
    meta: dict | None = None,
) -> TaskArtifact:
    ref = ref or f"{task_id}/{kind}"
    row = TaskArtifact(
        task_id=task_id,
        kind=kind,
        ref=ref,
        meta=meta or {},
        size_bytes=0,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def _link(session, parent_id, child_id) -> None:
    session.add(TaskDependency(
        parent_task_id=parent_id,
        child_task_id=child_id,
        created_at=datetime.now(UTC),
    ))
    await session.flush()


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_parents_returns_empty():
    sm = await get_sessionmaker_for_tests()
    async with sm() as session:
        ws = await _make_workspace(session, f"inj-np-{uuid4().hex[:6]}")
        child = await _make_task(session, ws.id, status="pending", title="child")
        await session.commit()

        store = _FakeStore({})
        result = await collect_dependency_outputs(
            session, child_task_id=child.id, artifact_store=store
        )
        assert result == []


@pytest.mark.asyncio
async def test_single_parent_git_diff_included():
    sm = await get_sessionmaker_for_tests()
    async with sm() as session:
        ws = await _make_workspace(session, f"inj-sp-{uuid4().hex[:6]}")
        parent = await _make_task(session, ws.id, status="done", title="Parent A")
        child = await _make_task(session, ws.id, status="blocked", title="Child B")
        art = await _add_artifact(session, parent.id, kind="git-diff", ref=f"{parent.id}/git-diff")
        await _link(session, parent.id, child.id)
        await session.commit()

        content = b"diff --git a/foo.py b/foo.py\n+new line\n"
        store = _FakeStore({str(art.ref): content})

        result = await collect_dependency_outputs(
            session, child_task_id=child.id, artifact_store=store
        )

        assert len(result) == 1
        out = result[0]
        assert out["parent_task_id"] == str(parent.id)
        assert out["parent_title"] == "Parent A"
        assert out["parent_status"] == "done"
        assert len(out["artifacts"]) == 1
        art_out = out["artifacts"][0]
        assert art_out["kind"] == "git-diff"
        assert art_out["content"] == content.decode()
        assert art_out["truncated"] is False


@pytest.mark.asyncio
async def test_oversize_artifact_is_truncated():
    sm = await get_sessionmaker_for_tests()
    async with sm() as session:
        ws = await _make_workspace(session, f"inj-trunc-{uuid4().hex[:6]}")
        parent = await _make_task(session, ws.id, status="done")
        child = await _make_task(session, ws.id, status="blocked")
        art = await _add_artifact(session, parent.id, kind="git-diff", ref=f"{parent.id}/git-diff")
        await _link(session, parent.id, child.id)
        await session.commit()

        big_content = b"x" * (PER_ARTIFACT_CAP + 500)
        store = _FakeStore({str(art.ref): big_content})

        result = await collect_dependency_outputs(
            session, child_task_id=child.id, artifact_store=store
        )

        assert len(result) == 1
        art_out = result[0]["artifacts"][0]
        assert art_out["truncated"] is True
        assert "[...truncated]" in art_out["content"]
        # Truncated content should not exceed per-artifact cap plus marker
        assert len(art_out["content"]) <= PER_ARTIFACT_CAP + len("\n[...truncated]") + 5


@pytest.mark.asyncio
async def test_total_cap_drops_oldest_parent():
    """Two parents whose artifacts sum over TOTAL_CAP — newest kept, oldest dropped."""
    sm = await get_sessionmaker_for_tests()
    async with sm() as session:
        ws = await _make_workspace(session, f"inj-cap-{uuid4().hex[:6]}")
        # Create oldest parent first (lower created_at / no finished_at means sort puts it last)
        old_parent = await _make_task(session, ws.id, status="done", title="OldParent")
        # Give new parent a later finished_at by setting it explicitly
        new_parent = Task(
            workspace_id=ws.id,
            title="NewParent",
            prompt="x",
            origin="web",
            agent="claude-code",
            status="done",
            finished_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        session.add(new_parent)
        await session.flush()

        child = await _make_task(session, ws.id, status="blocked")
        old_art = await _add_artifact(
            session, old_parent.id, kind="git-diff", ref=f"{old_parent.id}/git-diff"
        )
        new_art = await _add_artifact(
            session, new_parent.id, kind="git-diff", ref=f"{new_parent.id}/git-diff"
        )
        await _link(session, old_parent.id, child.id)
        await _link(session, new_parent.id, child.id)
        await session.commit()

        # Each artifact is 30 KB — two together exceed 50 KB TOTAL_CAP
        chunk = b"y" * (30 * 1024)
        store = _FakeStore({
            str(old_art.ref): chunk,
            str(new_art.ref): chunk,
        })

        result = await collect_dependency_outputs(
            session, child_task_id=child.id, artifact_store=store
        )

        # Newest parent should appear; oldest may be partially/fully excluded.
        parent_ids = {r["parent_task_id"] for r in result}
        assert str(new_parent.id) in parent_ids
        # Total content must not exceed TOTAL_CAP by more than marker overhead
        total = sum(
            len(a["content"])
            for r in result
            for a in r["artifacts"]
        )
        assert total <= TOTAL_CAP + 50  # small margin for truncation markers


@pytest.mark.asyncio
async def test_artifact_fetch_failure_skipped():
    sm = await get_sessionmaker_for_tests()
    async with sm() as session:
        ws = await _make_workspace(session, f"inj-fail-{uuid4().hex[:6]}")
        parent = await _make_task(session, ws.id, status="done")
        child = await _make_task(session, ws.id, status="blocked")
        bad_art = await _add_artifact(
            session, parent.id, kind="git-diff", ref=f"{parent.id}/missing"
        )
        good_art = await _add_artifact(
            session, parent.id, kind="git-branch", ref=f"{parent.id}/git-branch"
        )
        await _link(session, parent.id, child.id)
        await session.commit()

        _ = bad_art  # missing in store
        store = _FakeStore({str(good_art.ref): b"main"})

        result = await collect_dependency_outputs(
            session, child_task_id=child.id, artifact_store=store
        )

        # Parent still appears, but only the good artifact is included.
        assert len(result) == 1
        kinds = {a["kind"] for a in result[0]["artifacts"]}
        assert "git-branch" in kinds
        assert "git-diff" not in kinds


@pytest.mark.asyncio
async def test_log_archive_kind_skipped():
    """log-archive is not in ARTIFACT_KINDS_INJECTED — must be excluded."""
    assert "log-archive" not in ARTIFACT_KINDS_INJECTED

    sm = await get_sessionmaker_for_tests()
    async with sm() as session:
        ws = await _make_workspace(session, f"inj-skip-{uuid4().hex[:6]}")
        parent = await _make_task(session, ws.id, status="done")
        child = await _make_task(session, ws.id, status="blocked")
        art = await _add_artifact(
            session, parent.id, kind="log-archive", ref=f"{parent.id}/log-archive"
        )
        await _link(session, parent.id, child.id)
        await session.commit()

        store = _FakeStore({str(art.ref): b"lots of logs"})

        result = await collect_dependency_outputs(
            session, child_task_id=child.id, artifact_store=store
        )

        # Parent appears but has no artifacts (log-archive excluded).
        assert len(result) == 1
        assert result[0]["artifacts"] == []
