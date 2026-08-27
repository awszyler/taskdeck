from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID  # noqa: TCH003

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPkMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryChunk(Base, UUIDPkMixin):
    __tablename__ = "memory_chunks"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    source_kind: Mapped[str] = mapped_column(String(32))
    source_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    source_artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
