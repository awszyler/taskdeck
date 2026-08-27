from __future__ import annotations

from datetime import datetime  # noqa: TCH003
from uuid import UUID  # noqa: TCH003

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Sandbox(Base):
    """One row per task that has ever had a sandbox provisioned. The
    row is upserted on each /start (so we don't accumulate history
    rows per attempt — task_events captures the lifecycle if needed).
    Cascade-deleted with the parent task."""

    __tablename__ = "sandboxes"

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True,
    )
    # provisioning | running | stopping | stopped | error
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime: Mapped[str | None] = mapped_column(String(16), nullable=True)
    base_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
