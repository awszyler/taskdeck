from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from taskdeck_core.db.models import Task, Workspace
from taskdeck_core.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    settings = Settings()  # type: ignore[call-arg]
    # Use the main database. Each test wraps its work in a transaction that's rolled back.
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        trans = await conn.begin()
        SessionLocal = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with SessionLocal() as sess:
            yield sess
        await trans.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def workspace(session: AsyncSession) -> Workspace:
    ws = Workspace(slug=f"ws-{uuid4().hex[:8]}", name="test")
    session.add(ws)
    await session.flush()
    return ws


@pytest_asyncio.fixture
async def draft_task(session: AsyncSession, workspace: Workspace) -> Task:
    # Named "draft_task" for historical reasons; DRAFT no longer exists.
    # parse_failed is the editable pre-run state that resubmits to pending.
    task = Task(
        workspace_id=workspace.id,
        title="test",
        prompt="echo hi",
        origin="web",
        agent="shell",
        status="parse_failed",
    )
    session.add(task)
    await session.flush()
    return task
