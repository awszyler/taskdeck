"""CRP (Taskdeck Runner Protocol) message schemas.

All messages are discriminated by the `type` literal field.
Use `parse_message(dict)` to get the correct subclass.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTO_VERSION = "2.3"


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hello(_Msg):
    type: Literal["hello"] = "hello"
    runner_id: str
    capabilities: list[str]
    capability_descriptions: dict[str, str] = Field(default_factory=dict)
    max_parallel: int = Field(ge=1)
    isolation_modes: list[str]
    version: str


class Welcome(_Msg):
    type: Literal["welcome"] = "welcome"
    heartbeat_interval: int = Field(ge=1, default=15)


class Heartbeat(_Msg):
    type: Literal["heartbeat"] = "heartbeat"


class HeartbeatAck(_Msg):
    type: Literal["heartbeat.ack"] = "heartbeat.ack"


class DependencyArtifact(_Msg):
    kind: str
    content: str
    meta: dict[str, str] = Field(default_factory=dict)
    truncated: bool = False


class DependencyOutputItem(_Msg):
    """An entry from a parent task's .taskdeck/output.yml manifest.

    Tells the child agent what files the parent produced + where they
    are on disk relative to the child's cwd. Lets dependency carry
    real content beyond the original 3 git artifact kinds.
    """
    kind: str           # "interactive" | "document" | "code" | "data" | "image" | "archive"
    entry: str          # path relative to the parent's workspace root
    label: str = ""


class DependencyOutput(_Msg):
    parent_task_id: str
    parent_title: str
    parent_status: Literal["done", "failed", "cancelled"]
    artifacts: list[DependencyArtifact] = Field(default_factory=list)
    # Path relative to the child's cwd at runtime — typically
    # `../<parent_task_id>/`. Empty when parent has no on-disk worktree
    # (e.g. parent ran with no repo + workspace was already pruned).
    parent_workspace_path: str = ""
    # Entries from parent's output.yml manifest. Empty list = parent
    # didn't produce or didn't write a manifest.
    parent_outputs: list[DependencyOutputItem] = Field(default_factory=list)


class TaskAttachment(_Msg):
    """Multimodal task input (P7) — a user-uploaded file referenced
    in the task envelope. The runner downloads it from S3 (via core's
    /api/v1/attachments/<id>/file proxy) and writes it to
    cwd/.taskdeck/inputs/<filename> before the agent starts."""
    id: str
    filename: str
    content_type: str
    size_bytes: int


class MemoryChunk(_Msg):
    chunk_id: str
    source_kind: str
    text: str
    score: float
    source_task_id: str | None = None


class PriorTurn(_Msg):
    role: Literal["agent", "user"]
    content: str = Field(min_length=1, max_length=8192)


class TaskAssignPayload(_Msg):
    agent: str
    workspace_slug: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
        description="Logical workspace slug; runner uses this to isolate filesystem paths.",
    )
    repo: str | None = None
    base_branch: str = "main"
    prompt: str
    isolation: Literal["worktree", "docker"] = "worktree"
    timeout_seconds: int = Field(ge=1, default=7200)
    dependency_outputs: list[DependencyOutput] = Field(default_factory=list)
    memory: list[MemoryChunk] = Field(default_factory=list)
    prior_turns: list[PriorTurn] = Field(default_factory=list)
    attachments: list[TaskAttachment] = Field(default_factory=list)


class TaskAssign(_Msg):
    type: Literal["task.assign"] = "task.assign"
    task_id: str
    payload: TaskAssignPayload


class TaskAck(_Msg):
    type: Literal["task.ack"] = "task.ack"
    task_id: str


class TaskStarted(_Msg):
    type: Literal["task.started"] = "task.started"
    task_id: str


class TaskLog(_Msg):
    type: Literal["task.log"] = "task.log"
    task_id: str
    seq: int = Field(ge=0)
    stream: Literal["stdout", "stderr"]
    data: str


class TaskFinished(_Msg):
    type: Literal["task.finished"] = "task.finished"
    task_id: str
    exit_code: int
    summary: str | None = None


class TaskFailed(_Msg):
    type: Literal["task.failed"] = "task.failed"
    task_id: str
    reason: str
    detail: str | None = None


class TaskCancel(_Msg):
    type: Literal["task.cancel"] = "task.cancel"
    task_id: str


class TaskCancelled(_Msg):
    type: Literal["task.cancelled"] = "task.cancelled"
    task_id: str


class TaskAwaitingInput(_Msg):
    type: Literal["task.awaiting_input"] = "task.awaiting_input"
    task_id: str
    question: str = Field(min_length=1, max_length=8192)


CRPMessage = Annotated[
    Hello | Welcome | Heartbeat | HeartbeatAck | TaskAssign | TaskAck | TaskStarted | TaskLog | TaskFinished | TaskFailed | TaskCancel | TaskCancelled | TaskAwaitingInput,
    Field(discriminator="type"),
]


_adapter: TypeAdapter[CRPMessage] = TypeAdapter(CRPMessage)


def parse_message(data: dict[str, Any]) -> CRPMessage:
    """Parse a dict into the correct CRP message subclass."""
    return _adapter.validate_python(data)
