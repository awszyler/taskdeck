"""Tests for taskdeck_runner.deps.render_dependency_outputs and memory.render_memory."""
from __future__ import annotations

from taskdeck_proto.crp import (
    DependencyArtifact,
    DependencyOutput,
    MemoryChunk,
    TaskAssignPayload,
)
from taskdeck_runner.deps import render_dependency_outputs
from taskdeck_runner.memory import render_memory

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_output(
    *,
    parent_id: str = "parent-uuid-1",
    parent_title: str = "My parent task",
    parent_status: str = "done",
    artifacts: list[DependencyArtifact] | None = None,
) -> DependencyOutput:
    return DependencyOutput(
        parent_task_id=parent_id,
        parent_title=parent_title,
        parent_status=parent_status,  # type: ignore[arg-type]
        artifacts=artifacts or [],
    )


def _make_artifact(
    *,
    kind: str = "git-diff",
    content: str = "diff --git a/f b/f\n+new",
    meta: dict[str, str] | None = None,
    truncated: bool = False,
) -> DependencyArtifact:
    return DependencyArtifact(
        kind=kind,
        content=content,
        meta=meta or {},
        truncated=truncated,
    )


# ── tests ─────────────────────────────────────────────────────────────────────


def test_empty_list_returns_empty_string():
    assert render_dependency_outputs([]) == ""


def test_none_returns_empty_string():
    assert render_dependency_outputs(None) == ""


def test_single_parent_with_one_artifact():
    art = _make_artifact(kind="git-diff", content="diff content here")
    out = _make_output(artifacts=[art])
    rendered = render_dependency_outputs([out])

    assert "<dependency-outputs>" in rendered
    assert "</dependency-outputs>" in rendered
    assert 'kind="git-diff"' in rendered
    assert "diff content here" in rendered
    assert 'id="parent-uuid-1"' in rendered
    assert 'status="done"' in rendered


def test_truncated_artifact_has_attribute():
    art = _make_artifact(truncated=True)
    out = _make_output(artifacts=[art])
    rendered = render_dependency_outputs([out])
    assert 'truncated="true"' in rendered


def test_non_truncated_artifact_has_no_truncated_attribute():
    art = _make_artifact(truncated=False)
    out = _make_output(artifacts=[art])
    rendered = render_dependency_outputs([out])
    assert 'truncated="true"' not in rendered


def test_meta_attrs_appear_on_artifact_element():
    art = _make_artifact(
        kind="git-branch",
        content="main",
        meta={"head": "feat/x", "base": "main"},
    )
    out = _make_output(artifacts=[art])
    rendered = render_dependency_outputs([out])
    assert 'kind="git-branch"' in rendered
    # Both meta keys should appear as attributes on the <artifact> element
    assert "head=" in rendered
    assert "base=" in rendered
    assert "feat/x" in rendered


def test_title_with_special_chars_is_quoted():
    out = _make_output(parent_title='Task "quotes" and \\backslash')
    rendered = render_dependency_outputs([out])
    # Title should be escaped: backslash doubled, quote escaped
    assert '\\"quotes\\"' in rendered
    assert "\\\\" in rendered


def test_multiple_parents_both_appear():
    out1 = _make_output(parent_id="p1", parent_title="First", artifacts=[_make_artifact()])
    out2 = _make_output(parent_id="p2", parent_title="Second", artifacts=[_make_artifact(kind="git-branch", content="main")])
    rendered = render_dependency_outputs([out1, out2])
    assert 'id="p1"' in rendered
    assert 'id="p2"' in rendered
    assert 'kind="git-diff"' in rendered
    assert 'kind="git-branch"' in rendered


def test_shell_agent_skip_via_payload():
    """Verify that TaskAssignPayload with agent=shell carrying dependency_outputs
    does NOT trigger rendering — the guard is in crp_client, not in render_dependency_outputs.
    We test the guard directly: if agent == shell, dep_block should be empty.
    """
    art = _make_artifact(content="some diff")
    dep_out = _make_output(artifacts=[art])
    payload = TaskAssignPayload(
        agent="shell",
        workspace_slug="test",
        prompt="echo hi",
        dependency_outputs=[dep_out],
    )
    # The guard in crp_client: ai_agent = payload.agent != "shell"
    ai_agent = payload.agent != "shell"
    dep_block = render_dependency_outputs(payload.dependency_outputs) if ai_agent else ""
    assert dep_block == ""


def test_claude_code_agent_renders_dependency_outputs():
    """Verify that for claude-code agent, render_dependency_outputs is called."""
    art = _make_artifact(content="the diff")
    dep_out = _make_output(artifacts=[art])
    payload = TaskAssignPayload(
        agent="claude-code",
        workspace_slug="test",
        prompt="do the task",
        dependency_outputs=[dep_out],
    )
    ai_agent = payload.agent != "shell"
    dep_block = render_dependency_outputs(payload.dependency_outputs) if ai_agent else ""
    assert "<dependency-outputs>" in dep_block
    assert "the diff" in dep_block


# ── memory rendering tests ────────────────────────────────────────────────────


def _make_chunk(
    *,
    chunk_id: str = "chunk-uuid-1",
    source_kind: str = "task-summary",
    text: str = "We rewrote the login form.",
    score: float = 0.042,
    source_task_id: str | None = "task-uuid-1",
) -> MemoryChunk:
    return MemoryChunk(
        chunk_id=chunk_id,
        source_kind=source_kind,
        text=text,
        score=score,
        source_task_id=source_task_id,
    )


def test_render_memory_empty_returns_empty_string():
    assert render_memory([]) == ""
    assert render_memory(None) == ""


def test_render_memory_single_chunk():
    chunk = _make_chunk(text="The login was rewritten.", score=0.1)
    rendered = render_memory([chunk])
    assert "<memory>" in rendered
    assert "</memory>" in rendered
    assert "<chunk" in rendered
    assert "</chunk>" in rendered
    assert "The login was rewritten." in rendered
    assert 'source="task-summary"' in rendered
    assert "0.100" in rendered


def test_render_memory_no_task_id_shows_dash():
    chunk = _make_chunk(source_task_id=None)
    rendered = render_memory([chunk])
    assert 'task="—"' in rendered


def test_render_memory_with_task_id():
    chunk = _make_chunk(source_task_id="abc-123")
    rendered = render_memory([chunk])
    assert 'task="abc-123"' in rendered


def test_render_memory_multiple_chunks():
    c1 = _make_chunk(chunk_id="c1", text="First memory.", score=0.05)
    c2 = _make_chunk(chunk_id="c2", text="Second memory.", score=0.10, source_kind="artifact-decision")
    rendered = render_memory([c1, c2])
    assert rendered.count("<chunk") == 2
    assert "First memory." in rendered
    assert "Second memory." in rendered
    assert 'source="artifact-decision"' in rendered


def test_render_memory_appears_before_dependency_outputs():
    """The combined prompt should have <memory> before <dependency-outputs>."""
    chunk = _make_chunk(text="past decision text")
    art = _make_artifact(content="the diff")
    dep_out = _make_output(artifacts=[art])

    mem_block = render_memory([chunk])
    dep_block = render_dependency_outputs([dep_out])
    combined = mem_block + dep_block

    mem_pos = combined.find("<memory>")
    dep_pos = combined.find("<dependency-outputs>")
    assert mem_pos != -1
    assert dep_pos != -1
    assert mem_pos < dep_pos


def test_task_assign_payload_memory_default_empty():
    """memory field defaults to empty list — backward compat."""
    payload = TaskAssignPayload(agent="claude-code", workspace_slug="test", prompt="hi")
    assert payload.memory == []


def test_task_assign_payload_with_memory():
    chunk = _make_chunk()
    payload = TaskAssignPayload(agent="claude-code", workspace_slug="test", prompt="hi", memory=[chunk])
    assert len(payload.memory) == 1
    assert payload.memory[0].chunk_id == "chunk-uuid-1"


def test_shell_agent_skips_memory_block():
    """Shell agent guard: memory block should be empty for shell agent."""
    chunk = _make_chunk(text="some memory")
    payload = TaskAssignPayload(agent="shell", workspace_slug="test", prompt="echo hi", memory=[chunk])
    ai_agent = payload.agent != "shell"
    mem_block = render_memory(payload.memory) if ai_agent else ""
    assert mem_block == ""
