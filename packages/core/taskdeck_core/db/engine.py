from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TC002 — needed at runtime for FastAPI dependency resolution
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from taskdeck_core.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

log = logging.getLogger(__name__)


@asynccontextmanager
async def db_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan context manager that owns the DB engine lifecycle."""
    settings = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(settings.database_url, echo=False)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app.state.db_engine = engine
    app.state.db_sessionmaker = sm
    try:
        recovered = await _recover_orphan_tasks(sm)
        log.info("startup: marked %d orphan task(s) failed", recovered)
        yield
    finally:
        await engine.dispose()


async def _recover_orphan_tasks(sm: async_sessionmaker) -> int:
    """Mark any RUNNING tasks as FAILED with reason='core_restart'."""
    from sqlalchemy import select

    from taskdeck_core.db.models import Task
    from taskdeck_core.state.machine import TaskStatus
    from taskdeck_core.state.service import TaskStateService

    async with sm() as sess:
        rows = (
            await sess.scalars(
                select(Task).where(Task.status == TaskStatus.RUNNING.value)
            )
        ).all()
        svc = TaskStateService(sess)  # no publisher at boot — nobody is subscribed yet
        for t in rows:
            await svc.transition(
                t.id, TaskStatus.FAILED, actor="system", reason="core_restart"
            )
        await sess.commit()
        return len(rows)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:  # FastAPI dependency
    sm = request.app.state.db_sessionmaker
    async with sm() as session:
        yield session


async def get_sessionmaker_for_tests() -> async_sessionmaker:
    """Test-only helper — creates its own engine each call so pytest tests
    that don't boot the full app can still hit the DB without sharing state
    with other tests."""
    settings = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(settings.database_url)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
