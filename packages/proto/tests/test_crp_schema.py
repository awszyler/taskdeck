import pytest
from pydantic import ValidationError
from taskdeck_proto.crp import (
    DependencyArtifact,
    DependencyOutput,
    Heartbeat,
    Hello,
    MemoryChunk,
    TaskAssignPayload,
    TaskFinished,
    TaskLog,
    parse_message,
)


def test_hello_roundtrip():
    msg = Hello(
        runner_id="r-1",
        capabilities=["shell"],
        max_parallel=2,
        isolation_modes=["worktree"],
        version="0.0.1",
    )
    data = msg.model_dump()
    assert data["type"] == "hello"
    back = parse_message(data)
    assert isinstance(back, Hello)
    assert back.runner_id == "r-1"
    assert back.capabilities == ["shell"]


def test_hello_capability_descriptions_optional_default_empty():
    """Old runners without the new field still validate."""
    msg = parse_message({
        "type": "hello",
        "runner_id": "old",
        "capabilities": ["shell"],
        "max_parallel": 1,
        "isolation_modes": ["worktree"],
        "version": "0.0.1",
    })
    assert isinstance(msg, Hello)
    assert msg.capability_descriptions == {}


def test_hello_capability_descriptions_roundtrip():
    msg = Hello(
        runner_id="r-2",
        capabilities=["shell", "claude-code"],
        capability_descriptions={
            "shell": "Single shell command.",
            "claude-code": "AI coding agent.",
        },
        max_parallel=2,
        isolation_modes=["worktree"],
        version="0.0.1",
    )
    back = parse_message(msg.model_dump())
    assert isinstance(back, Hello)
    assert back.capability_descriptions["claude-code"] == "AI coding agent."


def test_task_assign_payload_defaults():
    payload = TaskAssignPayload(agent="shell", workspace_slug="test", prompt="echo hi")
    assert payload.isolation == "worktree"
    assert payload.timeout_seconds == 7200
    assert payload.repo is None
    assert payload.base_branch == "main"


def test_task_log_seq_is_non_negative():
    with pytest.raises(ValidationError):
        TaskLog(task_id="t-1", seq=-1, stream="stdout", data="x")


def test_parse_message_unknown_type_raises():
    with pytest.raises(ValidationError):
        parse_message({"type": "nonexistent"})


def test_discriminated_union_picks_correct_class():
    raw = {
        "type": "task.finished",
        "task_id": "t-1",
        "exit_code": 0,
        "summary": "done",
    }
    msg = parse_message(raw)
    assert isinstance(msg, TaskFinished)
    assert msg.exit_code == 0


def test_heartbeat_has_no_required_fields_beyond_type():
    msg = Heartbeat()
    assert msg.type == "heartbeat"


# ── P2.2.1: dependency_outputs schema tests ─────────────────────────────────


def test_task_assign_payload_no_dependency_outputs_backward_compat():
    """Parsing without dependency_outputs still works — back-compat."""
    payload = TaskAssignPayload(agent="claude-code", workspace_slug="ws-1", prompt="do the thing")
    assert payload.dependency_outputs == []


def test_task_assign_payload_with_dependency_outputs():
    artifact = DependencyArtifact(kind="git-diff", content="diff --git a/f b/f\n+line")
    dep_out = DependencyOutput(
        parent_task_id="abc-123",
        parent_title="Parent task",
        parent_status="done",
        artifacts=[artifact],
    )
    payload = TaskAssignPayload(
        agent="claude-code",
        workspace_slug="ws-1",
        prompt="child task",
        dependency_outputs=[dep_out],
    )
    assert len(payload.dependency_outputs) == 1
    assert payload.dependency_outputs[0].parent_task_id == "abc-123"
    assert payload.dependency_outputs[0].artifacts[0].kind == "git-diff"


def test_dependency_output_parent_status_rejects_unknown():
    with pytest.raises(ValidationError):
        DependencyOutput(
            parent_task_id="x",
            parent_title="t",
            parent_status="running",  # not in Literal["done","failed","cancelled"]
            artifacts=[],
        )


def test_dependency_artifact_empty_meta_defaults():
    art = DependencyArtifact(kind="git-branch", content="main")
    assert art.meta == {}
    assert art.truncated is False


def test_dependency_artifact_roundtrip():
    art = DependencyArtifact(
        kind="git-diff",
        content="some diff",
        meta={"branch": "feat/x"},
        truncated=True,
    )
    data = art.model_dump()
    back = DependencyArtifact(**data)
    assert back.kind == "git-diff"
    assert back.meta == {"branch": "feat/x"}
    assert back.truncated is True


# ── P3.3: MemoryChunk schema tests ───────────────────────────────────────────


def test_memory_chunk_required_fields():
    chunk = MemoryChunk(
        chunk_id="c-1",
        source_kind="task-summary",
        text="We refactored the auth flow.",
        score=0.042,
    )
    assert chunk.chunk_id == "c-1"
    assert chunk.source_kind == "task-summary"
    assert chunk.source_task_id is None


def test_memory_chunk_with_source_task_id():
    chunk = MemoryChunk(
        chunk_id="c-2",
        source_kind="artifact-decision",
        text="Use postgres.",
        score=0.1,
        source_task_id="task-abc",
    )
    assert chunk.source_task_id == "task-abc"


def test_task_assign_payload_memory_defaults_empty():
    payload = TaskAssignPayload(agent="shell", workspace_slug="test", prompt="echo hi")
    assert payload.memory == []


def test_task_assign_payload_memory_populated():
    chunk = MemoryChunk(
        chunk_id="m-1",
        source_kind="task-summary",
        text="Past context.",
        score=0.05,
        source_task_id="task-x",
    )
    payload = TaskAssignPayload(
        agent="claude-code",
        workspace_slug="ws-1",
        prompt="do stuff",
        memory=[chunk],
    )
    assert len(payload.memory) == 1
    assert payload.memory[0].score == 0.05


def test_task_assign_payload_with_memory_roundtrip():
    chunk = MemoryChunk(
        chunk_id="m-2",
        source_kind="artifact-git-diff",
        text="diff --git ...",
        score=0.25,
    )
    payload = TaskAssignPayload(agent="claude-code", workspace_slug="ws-1", prompt="task", memory=[chunk])
    data = payload.model_dump()
    assert len(data["memory"]) == 1
    restored = TaskAssignPayload(**data)
    assert restored.memory[0].chunk_id == "m-2"
    assert restored.memory[0].source_task_id is None


# ── P5.3: workspace_slug schema tests ────────────────────────────────────────


def test_task_assign_round_trip_with_workspace_slug():
    """Encoding and decoding preserves workspace_slug."""
    from taskdeck_proto.crp import TaskAssign, TaskAssignPayload

    payload = TaskAssignPayload(
        agent="shell",
        workspace_slug="alpha",
        prompt="echo hi",
    )
    msg = TaskAssign(task_id="t-1", payload=payload)
    raw = msg.model_dump_json()
    decoded = TaskAssign.model_validate_json(raw)
    assert decoded.payload.workspace_slug == "alpha"


def test_task_assign_missing_workspace_slug_raises():
    """A task.assign envelope without workspace_slug fails Pydantic validation."""
    from taskdeck_proto.crp import TaskAssign

    raw = '{"type":"task.assign","task_id":"t-1","payload":{"agent":"shell","prompt":"echo hi"}}'
    with pytest.raises(ValidationError):
        TaskAssign.model_validate_json(raw)


def test_task_assign_invalid_workspace_slug_raises():
    """Slugs containing path-traversal or uppercase characters are rejected."""
    for bad_slug in ["..", "foo/bar", "with space", "Capital", "-leading-dash", "x" * 65]:
        with pytest.raises(ValidationError):
            TaskAssignPayload(agent="shell", workspace_slug=bad_slug, prompt="x")


# ── P5.4: TaskAwaitingInput + prior_turns schema tests ───────────────────────


def test_task_awaiting_input_round_trip():
    """Encoding and decoding preserves the awaiting_input payload."""
    from taskdeck_proto.crp import TaskAwaitingInput

    msg = TaskAwaitingInput(task_id="t-1", question="May I read README.md?")
    raw = msg.model_dump_json()
    decoded = parse_message(msg.model_dump())
    assert isinstance(decoded, TaskAwaitingInput)
    assert decoded.task_id == "t-1"
    assert decoded.question == "May I read README.md?"


def test_task_assign_payload_with_prior_turns_round_trip():
    """prior_turns array round-trips and preserves order + roles."""
    from taskdeck_proto.crp import PriorTurn, TaskAssign, TaskAssignPayload

    payload = TaskAssignPayload(
        agent="claude-code",
        workspace_slug="alpha",
        prompt="continue task",
        prior_turns=[
            PriorTurn(role="agent", content="May I read?"),
            PriorTurn(role="user", content="yes"),
        ],
    )
    msg = TaskAssign(task_id="t-1", payload=payload)
    raw = msg.model_dump_json()
    decoded = TaskAssign.model_validate_json(raw)
    assert len(decoded.payload.prior_turns) == 2
    assert decoded.payload.prior_turns[0].role == "agent"
    assert decoded.payload.prior_turns[0].content == "May I read?"
    assert decoded.payload.prior_turns[1].role == "user"


def test_prior_turn_role_validation():
    """Only 'agent' or 'user' roles are accepted."""
    from taskdeck_proto.crp import PriorTurn

    PriorTurn(role="agent", content="x")
    PriorTurn(role="user", content="x")
    with pytest.raises(ValidationError):
        PriorTurn(role="system", content="x")
