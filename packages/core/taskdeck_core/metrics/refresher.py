"""Background gauge refresher.

`ccpt_tasks_by_status` is a snapshot gauge: refreshing it every 30s from the
DB is much cheaper than emitting an event on every change, and the metric is
fundamentally a "current state of the world" view.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from taskdeck_core.metrics.registry import TASKS_BY_STATUS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

log = logging.getLogger(__name__)

ALL_STATUSES = (
    "parsing", "parse_failed", "pending", "blocked",
    "running", "awaiting_input", "in_review", "done", "failed", "cancelled",
)


async def refresh_once(sessionmaker: async_sessionmaker) -> None:
    async with sessionmaker() as s:
        rows = (await s.execute(text("SELECT status, COUNT(*) FROM tasks GROUP BY 1"))).all()
    seen: dict[str, int] = {}
    for status, n in rows:
        seen[str(status)] = int(n)
    for st in ALL_STATUSES:
        TASKS_BY_STATUS.labels(status=st).set(seen.get(st, 0))


async def run_forever(sessionmaker: async_sessionmaker, *, interval_seconds: float = 30.0) -> None:
    while True:
        try:
            await refresh_once(sessionmaker)
        except Exception:  # noqa: BLE001
            log.exception("tasks_by_status refresh failed")
        await asyncio.sleep(interval_seconds)
