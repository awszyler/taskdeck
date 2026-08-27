"""Tests for ccpt_* business metrics.

Each test does its own counter delta (snapshot before / inc / snapshot after)
because Prometheus client metrics are process-wide singletons; tests can run
in any order and we can't reset them.
"""
from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from taskdeck_core.crp.hub import RunnerConnection, RunnerHub
from taskdeck_core.memory.embedding import BedrockEmbeddingClient
from taskdeck_core.metrics.registry import (
    COST_EVENTS_TOTAL,
    LLM_CALL_DURATION_SECONDS,
    RUNNERS_CONNECTED,
    TASK_STATE_TRANSITIONS_TOTAL,
)
from taskdeck_core.state.machine import TaskStatus
from taskdeck_core.state.service import TaskStateService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from taskdeck_core.db.models import Task


def _counter_value(counter, **labels) -> float:
    sample = counter.labels(**labels)
    return sample._value.get()  # type: ignore[no-untyped-call]


def _histogram_call_count(histogram, **labels) -> int:
    """Return the cumulative observation count for a histogram label-set."""
    target = labels
    for metric in histogram.collect():
        for s in metric.samples:
            if s.name.endswith("_count") and s.labels == {k: str(v) for k, v in target.items()}:
                return int(s.value)
    return 0


class _FakeSocket:
    async def send_json(self, data: dict) -> None: ...


def test_runner_register_unregister_updates_gauge():
    before = RUNNERS_CONNECTED._value.get()  # type: ignore[no-untyped-call]
    hub = RunnerHub()
    conn = RunnerConnection("r-metrics-1", _FakeSocket(), 1, ["shell"])  # type: ignore[arg-type]
    hub.register(conn)
    assert RUNNERS_CONNECTED._value.get() == before + 1  # type: ignore[no-untyped-call]
    # Re-registering same id must not double-count.
    hub.register(conn)
    assert RUNNERS_CONNECTED._value.get() == before + 1  # type: ignore[no-untyped-call]
    hub.unregister("r-metrics-1")
    assert RUNNERS_CONNECTED._value.get() == before  # type: ignore[no-untyped-call]
    # Unregister of non-existent must be a no-op.
    hub.unregister("r-metrics-1")
    assert RUNNERS_CONNECTED._value.get() == before  # type: ignore[no-untyped-call]


async def test_transition_increments_counter(session: AsyncSession, draft_task: Task):
    before = _counter_value(
        TASK_STATE_TRANSITIONS_TOTAL,
        from_status="parse_failed",
        to_status="pending",
        actor="user",
    )
    svc = TaskStateService(session)
    await svc.transition(draft_task.id, TaskStatus.PENDING, actor="user", reason="submit")
    after = _counter_value(
        TASK_STATE_TRANSITIONS_TOTAL,
        from_status="parse_failed",
        to_status="pending",
        actor="user",
    )
    assert after == before + 1


async def test_admin_transition_increments_counter(session: AsyncSession, draft_task: Task):
    before = _counter_value(
        TASK_STATE_TRANSITIONS_TOTAL,
        from_status="parse_failed",
        to_status="failed",
        actor="admin",
    )
    svc = TaskStateService(session)
    await svc.admin_transition(draft_task.id, TaskStatus.FAILED, actor="admin")
    after = _counter_value(
        TASK_STATE_TRANSITIONS_TOTAL,
        from_status="parse_failed",
        to_status="failed",
        actor="admin",
    )
    assert after == before + 1


@pytest.mark.asyncio
async def test_bedrock_embed_records_histogram():
    before = _histogram_call_count(LLM_CALL_DURATION_SECONDS, kind="embed", provider="bedrock")
    client = BedrockEmbeddingClient(model_id="cohere.embed-multilingual-v3", region="ap-northeast-1")
    fake_boto = MagicMock()
    fake_boto.invoke_model.return_value = {
        "body": MagicMock(read=lambda: _json.dumps({"embeddings": [[0.1] * 1024]}).encode())
    }
    client._client = fake_boto
    await client.embed_batch(["hello"])
    after = _histogram_call_count(LLM_CALL_DURATION_SECONDS, kind="embed", provider="bedrock")
    assert after == before + 1


def test_cost_events_counter_inc_does_not_explode_on_unknown_labels():
    """Sanity: COST_EVENTS_TOTAL with empty model still labels correctly."""
    before = _counter_value(COST_EVENTS_TOTAL, provider="test", model="unknown")
    COST_EVENTS_TOTAL.labels(provider="test", model="unknown").inc()
    after = _counter_value(COST_EVENTS_TOTAL, provider="test", model="unknown")
    assert after == before + 1
