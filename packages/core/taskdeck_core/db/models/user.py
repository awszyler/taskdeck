from __future__ import annotations

from uuid import UUID  # noqa: TCH003

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPkMixin


class User(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "users"

    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True
    )
    email: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32), default="member")

    # Cognito sub claim (P5.1). Nullable: synthetic local user in disabled
    # mode never has a sub; bootstrap rows may exist before a Cognito login
    # populates this column. Unique partial index in migration 0011.
    cognito_sub: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Legacy GitHub-era columns kept on the model for one release so old
    # rows (none in production but present in dev DBs) round-trip without
    # error. They are no longer populated by any code path.
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    login: Mapped[str | None] = mapped_column(String(64), nullable=True)
