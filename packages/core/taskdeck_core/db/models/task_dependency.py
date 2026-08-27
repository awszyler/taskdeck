from __future__ import annotations

from datetime import datetime  # noqa: TCH003
from uuid import UUID  # noqa: TCH003

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPkMixin


class TaskDependency(Base, UUIDPkMixin):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint("parent_task_id", "child_task_id", name="uq_task_dep_parent_child"),
        CheckConstraint("parent_task_id <> child_task_id", name="no_self_dep"),
    )

    parent_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    child_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
