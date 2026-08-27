from __future__ import annotations

from datetime import datetime  # noqa: TCH003
from uuid import UUID  # noqa: TCH003

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPkMixin


class Task(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "tasks"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))

    title: Mapped[str] = mapped_column(String(120))
    # One-or-two-sentence elaboration shown as the card tooltip / drawer
    # subtitle. NULL for old tasks until the backfill script fills them in.
    description: Mapped[str | None] = mapped_column(String(280), nullable=True)
    prompt: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16))
    raw_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    agent: Mapped[str] = mapped_column(String(32))
    repo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    base_branch: Mapped[str] = mapped_column(String(128), default="main")
    isolation: Mapped[str] = mapped_column(String(16), default="worktree")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=7200)

    status: Mapped[str] = mapped_column(String(16), default="parsing")
    assigned_runner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runners.id"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Historical: pointed at the source of the old `/tasks/{id}/retry`
    # endpoint (which created a new task with retry_of set). That endpoint
    # was retired in favor of in-place /rerun, but the column is kept so
    # already-persisted rows with non-NULL values stay valid. New rows
    # always have retry_of=NULL.
    retry_of: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Idempotency key for client-driven submit retries. Partial unique index on
    # (workspace_id, idempotency_key) enforces dedup; old tasks have NULL and
    # never participate. See migration 0010.
    idempotency_key: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)

    # P6.3.7 cold archive: when sandbox-host's workspace_gc tar.gz's a
    # 30-day-old workspace into S3, it POSTs the resulting key + timestamp
    # back to core to set these fields. The next sandbox-start sees a
    # non-NULL archive_key + missing on-disk dir, pulls the tarball back,
    # extracts, and provisions normally. NULL = workspace either still
    # on disk or never existed (pre-P6.3.7 historical row).
    archive_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TaskEvent(Base, UUIDPkMixin):
    __tablename__ = "task_events"

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"))
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(16))  # system | user | runner
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskLog(Base):
    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"))
    seq: Mapped[int] = mapped_column(Integer)
    stream: Mapped[str] = mapped_column(String(8))  # stdout | stderr
    data: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskArtifact(Base, UUIDPkMixin):
    __tablename__ = "task_artifacts"

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str] = mapped_column(String(32))
    ref: Mapped[str] = mapped_column(String(512))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskAttachment(Base, UUIDPkMixin):
    """User-uploaded multimodal input attached to a task (P7).

    task_id starts NULL when uploaded; the /tasks create endpoint sets
    it after the task row exists. Workspace_id is captured at upload
    time and re-checked when linking — prevents cross-workspace leaks
    if a malicious client crafts an attachment_id from another user.
    """
    __tablename__ = "task_attachments"

    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True,
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    original_filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
