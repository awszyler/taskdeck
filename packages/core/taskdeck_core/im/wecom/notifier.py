from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from taskdeck_core.db.models import ImIdentityLink, Task

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from taskdeck_core.im.wecom.client import WecomClient, _NoopWecomClient

log = logging.getLogger(__name__)


class WecomNotifier:
    """Subscribes to EventBus and sends WeCom notifications on task terminal states."""

    TERMINAL = {"done", "failed"}

    def __init__(
        self,
        *,
        client: WecomClient | _NoopWecomClient,
        sessionmaker: async_sessionmaker,
        public_base_url: str,
    ):
        self._client = client
        self._sm = sessionmaker
        self._public_base_url = public_base_url

    async def handle(self, event: dict) -> None:
        if event.get("type") != "task.event":
            return
        if event.get("to") not in self.TERMINAL:
            return
        task_id_raw = event.get("task_id")
        if not task_id_raw:
            return

        try:
            task_id = UUID(task_id_raw)
        except ValueError:
            return

        async with self._sm() as session:
            task = await session.get(Task, task_id)
            if task is None or task.origin != "im":
                return
            if task.created_by is None:
                return
            link_row = (await session.scalars(
                select(ImIdentityLink).where(
                    ImIdentityLink.user_id == task.created_by,
                    ImIdentityLink.platform == "wecom",
                )
            )).first()
            if link_row is None:
                return
            external_id = link_row.external_id
            short = str(task.id)[:8]
            status = event["to"]
            url = f"{self._public_base_url.rstrip('/')}/?task={task.id}"
            emoji = "✓" if status == "done" else "✗"
            reply = f"{emoji} Task #{short} {status}\n{task.title}\n{url}"

        try:
            await self._client.send_text(to_user=external_id, content=reply)
        except Exception as e:  # noqa: BLE001
            log.warning("wecom notify failed for task %s: %s", task_id, e)
