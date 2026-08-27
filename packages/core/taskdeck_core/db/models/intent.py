from __future__ import annotations

from datetime import datetime  # noqa: TCH003
from uuid import UUID  # noqa: TCH003

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPkMixin


class IntentParseLog(Base, UUIDPkMixin):
    __tablename__ = "intent_parse_log"

    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    raw_input: Mapped[str] = mapped_column(Text)
    parsed_output: Mapped[dict] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(128))
    latency_ms: Mapped[int] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
