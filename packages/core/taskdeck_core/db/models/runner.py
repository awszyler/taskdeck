from __future__ import annotations

from datetime import datetime  # noqa: TCH003
from uuid import UUID  # noqa: TCH003

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPkMixin


class Runner(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "runners"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String(128))
    capabilities: Mapped[list[str]] = mapped_column(JSON)
    max_parallel: Mapped[int] = mapped_column()
    isolation_modes: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="offline")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[str] = mapped_column(String(32), default="")
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RunnerAuthToken(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "runner_auth_tokens"

    runner_id: Mapped[UUID] = mapped_column(ForeignKey("runners.id"))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
