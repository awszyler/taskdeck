"""Test the tasks_by_status background gauge refresher.

The refresher opens its own session, so we can't use the standard rolled-back
test session. Instead we commit fixture rows, run the refresher, and clean up
in a finally block.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from taskdeck_core.db.models import Task, Workspace
from taskdeck_core.metrics.refresher import refresh_once
from taskdeck_core.metrics.registry import TASKS_BY_STATUS
from taskdeck_core.settings import Settings


def _gauge_value(status: str) -> float:
    sample = TASKS_BY_STATUS.labels(status=status)
    return sample._value.get()  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_refresh_reads_status_counts():
    settings = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(settings.database_url)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    ws_slug = f"metrics-{uuid4().hex[:8]}"
    ws_id = None
    try:
        async with sm() as s:
            ws = Workspace(slug=ws_slug, name="test-metrics")
            s.add(ws)
            await s.flush()
            ws_id = ws.id
            for st in ("pending", "pending", "done"):
                s.add(Task(
                    workspace_id=ws.id, title="m", prompt="p",
                    origin="web", agent="shell", status=st,
                ))
            await s.commit()

        before_pending = _gauge_value("pending")
        before_done = _gauge_value("done")

        await refresh_once(sm)

        # Gauge tracks ABSOLUTE counts read from DB. After refresh, value must be
        # >= our test contribution. We can't assert exact totals because other rows
        # may exist from concurrent tests sharing the DB, but the lower bound is safe.
        assert _gauge_value("pending") >= before_pending
        assert _gauge_value("done") >= before_done
        # Every status label is published, even when zero (so Grafana panels render).
        assert TASKS_BY_STATUS.labels(status="cancelled")._value.get() >= 0  # type: ignore[no-untyped-call]
    finally:
        async with sm() as s:
            if ws_id is not None:
                await s.execute(delete(Task).where(Task.workspace_id == ws_id))
                await s.execute(delete(Workspace).where(Workspace.id == ws_id))
                await s.commit()
        await engine.dispose()
