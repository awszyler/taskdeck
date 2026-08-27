from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from taskdeck_core.cost.pricing import Pricing
from taskdeck_core.cost.sink import CostEventSink
from taskdeck_core.db.engine import get_sessionmaker_for_tests
from taskdeck_core.db.models import CostEvent, Workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_workspace(sm) -> Workspace:
    async with sm() as s:
        ws = Workspace(slug=f"cst-{uuid4().hex[:8]}", name="cost-test")
        s.add(ws)
        await s.commit()
        await s.refresh(ws)
        return ws


async def _cleanup(sm, *workspace_ids) -> None:
    async with sm() as s:
        for wid in workspace_ids:
            # cascade deletes cost_events rows too
            ws = await s.get(Workspace, wid)
            if ws:
                await s.delete(ws)
        await s.commit()


def _make_sink(sm, *, enabled: bool = True) -> CostEventSink:
    return CostEventSink(sessionmaker=sm, pricing=Pricing.load(), enabled=enabled)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_event_persisted():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        sink = _make_sink(sm)
        event = {
            "type": "cost.event",
            "provider": "litellm",
            "operation": "intent_parser",
            "model": "anthropic/claude-sonnet-4-6",
            "tokens_in": 1000,
            "tokens_out": 500,
            "workspace_id": str(ws.id),
        }
        await sink.handle(event)

        async with sm() as s:
            rows = (await s.execute(select(CostEvent).where(CostEvent.workspace_id == ws.id))).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.provider == "litellm"
        assert row.operation == "intent_parser"
        assert row.model == "anthropic/claude-sonnet-4-6"
        assert row.tokens_in == 1000
        assert row.tokens_out == 500
        assert row.workspace_id == ws.id
        # (1000 * 3 + 500 * 15) / 1_000_000 = 0.0105
        assert row.cost_usd == Decimal("0.010500")
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_cost_event_unknown_model_inserts_without_cost():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        sink = _make_sink(sm)
        event = {
            "type": "cost.event",
            "provider": "litellm",
            "operation": "intent_parser",
            "model": "totally/unknown-model",
            "tokens_in": 100,
            "tokens_out": 50,
            "workspace_id": str(ws.id),
        }
        await sink.handle(event)

        async with sm() as s:
            rows = (await s.execute(select(CostEvent).where(CostEvent.workspace_id == ws.id))).scalars().all()

        assert len(rows) == 1
        assert rows[0].cost_usd is None
        assert rows[0].model == "totally/unknown-model"
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_cost_event_audio():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        sink = _make_sink(sm)
        event = {
            "type": "cost.event",
            "provider": "litellm",
            "operation": "stt",
            "model": "openai/whisper-1",
            "audio_seconds": 60.0,
            "workspace_id": str(ws.id),
        }
        await sink.handle(event)

        async with sm() as s:
            rows = (await s.execute(select(CostEvent).where(CostEvent.workspace_id == ws.id))).scalars().all()

        assert len(rows) == 1
        # 60 * 0.0001 = 0.006
        assert rows[0].cost_usd == Decimal("0.006000")
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_disabled_sink_does_not_insert():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        sink = _make_sink(sm, enabled=False)
        event = {
            "type": "cost.event",
            "provider": "litellm",
            "operation": "intent_parser",
            "model": "anthropic/claude-sonnet-4-6",
            "tokens_in": 1000,
            "tokens_out": 500,
            "workspace_id": str(ws.id),
        }
        await sink.handle(event)

        async with sm() as s:
            rows = (await s.execute(select(CostEvent).where(CostEvent.workspace_id == ws.id))).scalars().all()

        assert len(rows) == 0
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_non_cost_event_type_ignored():
    sm = await get_sessionmaker_for_tests()
    ws = await _make_workspace(sm)
    try:
        sink = _make_sink(sm)
        await sink.handle({"type": "task.updated", "id": str(uuid4())})

        async with sm() as s:
            rows = (await s.execute(select(CostEvent).where(CostEvent.workspace_id == ws.id))).scalars().all()

        assert len(rows) == 0
    finally:
        await _cleanup(sm, ws.id)


@pytest.mark.asyncio
async def test_no_workspace_id_still_inserts():
    sm = await get_sessionmaker_for_tests()
    try:
        sink = _make_sink(sm)
        event = {
            "type": "cost.event",
            "provider": "litellm",
            "operation": "intent_parser",
            "model": "anthropic/claude-sonnet-4-6",
            "tokens_in": 200,
            "tokens_out": 100,
        }
        await sink.handle(event)

        async with sm() as s:
            rows = (
                await s.execute(
                    select(CostEvent)
                    .where(CostEvent.workspace_id.is_(None))
                    .order_by(CostEvent.created_at.desc())
                    .limit(1)
                )
            ).scalars().all()

        assert len(rows) == 1
        assert rows[0].workspace_id is None
    finally:
        # Clean up the orphan row
        async with sm() as s:
            rows = (await s.execute(
                select(CostEvent).where(CostEvent.workspace_id.is_(None)).order_by(CostEvent.created_at.desc()).limit(1)
            )).scalars().all()
            for row in rows:
                await s.delete(row)
            await s.commit()
