from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from taskdeck_core.db.models import AuditEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

log = logging.getLogger(__name__)


class AuditEventSink:
    def __init__(self, *, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    async def handle(self, event: dict) -> None:
        if event.get("type") != "audit.event":
            return
        try:
            workspace_id = UUID(event["workspace_id"]) if event.get("workspace_id") else None
            user_id = UUID(event["user_id"]) if event.get("user_id") else None
            target_id = UUID(event["target_id"]) if event.get("target_id") else None
        except (ValueError, TypeError):
            return
        async with self._sm() as db:
            db.add(AuditEvent(
                workspace_id=workspace_id,
                user_id=user_id,
                kind=event.get("kind", "unknown"),
                target_type=event.get("target_type"),
                target_id=target_id,
                meta=event.get("meta", {}),
                created_at=datetime.now(UTC),
            ))
            await db.commit()
