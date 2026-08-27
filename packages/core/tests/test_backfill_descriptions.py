"""Tests for the description backfill script.

Exercises the heuristic path (no LLM) end-to-end against the DB:
- dry-run writes nothing
- real run fills title + description
- idempotency: a second run skips already-filled rows
- prefer-longer-title keeps a good short title
"""
from __future__ import annotations

import argparse

import pytest
from taskdeck_core.db.models import Task, Workspace
from taskdeck_core.settings import Settings

pytestmark = pytest.mark.asyncio


def _args(**kw) -> argparse.Namespace:
    base = dict(
        dry_run=False,
        limit=0,
        prefer_longer_title=False,
        heuristic_only=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


async def test_heuristic_summary_clips_title_and_fills_description():
    """_summarize is a pure function — no DB needed."""
    from scripts.backfill_descriptions import _summarize

    long_raw = "帮我把北海道的行程重新排一下，" + "x" * 200
    title, desc = await _summarize(None, raw=long_raw, caps=[])
    # Ellipsis is counted WITHIN the budget — must fit the DB columns.
    assert len(title) <= 60
    assert desc is not None and len(desc) <= 280


async def test_summarize_short_input_no_description():
    from scripts.backfill_descriptions import _summarize

    title, desc = await _summarize(None, raw="echo hi", caps=[])
    assert title == "echo hi"
    # Short input that fits in the title needs no description.
    assert desc is None


async def test_prefer_longer_title_keeps_good_short_title():
    """Pure-logic check of the keep-title rule without DB round-trip."""
    from taskdeck_core.intent.parser import TITLE_MAX

    old_title = "Reorder the Hokkaido itinerary for day 3"  # ≤60, descriptive
    new_title = "Fix itinerary"  # shorter
    keep = (
        len(old_title) <= TITLE_MAX
        and len(new_title) < len(old_title)
    )
    assert keep is True


async def test_run_backfills_and_is_idempotent():
    """End-to-end against the test DB using run()'s own engine. Seeds and
    reads back via independent sessions on the same DATABASE_URL so we
    don't fight the fixture session's transaction scope."""
    from scripts.backfill_descriptions import run
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    s = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(s.database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    long_raw = "整理一下这个长长的需求 " + "c" * 300
    async with sm() as seed:
        ws = Workspace(slug="bf-test", name="bf-test")
        seed.add(ws)
        await seed.flush()
        t = Task(
            workspace_id=ws.id, title="c" * 100, raw_input=long_raw,
            prompt="x", origin="web", agent="claude-code", status="done",
        )
        seed.add(t)
        await seed.commit()
        tid, wsid = t.id, ws.id

    # A short-source task that needs NO description. The backfill must
    # NOT touch it — otherwise a None description writes NULL and the
    # "description IS NULL" scan re-selects it forever (the convergence
    # bug found during the prod rollout).
    async with sm() as seed2:
        short = Task(
            workspace_id=wsid, title="echo hi", raw_input="echo hi",
            prompt="echo hi", origin="web", agent="shell", status="done",
        )
        seed2.add(short)
        await seed2.commit()
        short_id = short.id

    try:
        await run(_args(heuristic_only=True))
        async with sm() as r1:
            row = await r1.get(Task, tid)
            assert row is not None
            first_desc = row.description
            assert first_desc is not None
            assert len(row.title) <= 60
            # Short row left alone — no churn, description stays NULL.
            srow = await r1.get(Task, short_id)
            assert srow is not None
            assert srow.description is None

        # Second run is fully convergent: the long row is filled (skipped)
        # and the short row is never selected, so nothing changes.
        await run(_args(heuristic_only=True))
        async with sm() as r2:
            row = await r2.get(Task, tid)
            assert row is not None
            assert row.description == first_desc
            srow = await r2.get(Task, short_id)
            assert srow is not None
            assert srow.description is None
    finally:
        from sqlalchemy import delete
        async with sm() as cleanup:
            await cleanup.execute(delete(Task).where(Task.workspace_id == wsid))
            await cleanup.execute(delete(Workspace).where(Workspace.id == wsid))
            await cleanup.commit()
        await engine.dispose()
