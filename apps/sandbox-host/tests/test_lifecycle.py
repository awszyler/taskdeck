"""LifecycleService tests (P-H Phase 3).

Cover the per-task lock + idempotency contract:

- Two parallel provision_idempotent() for the same task_id only
  end up calling the underlying _raw_provision once.
- A second provision when the first row is healthy returns the
  existing record (no rebuild).
- A second provision when the row is dead does teardown + rebuild.
- Lock pool prunes on teardown.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sandbox_host.lifecycle import LifecycleService
from sandbox_host.state import SandboxRecord, SandboxRegistry


def _running_record(task_id: str = "t") -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        task_id=task_id,
        container_id=f"cid-{task_id}",
        container_name=f"td-sandbox-{task_id}",
        network_name=f"td-sandbox-net-{task_id}",
        host_port=10000,
        internal_port=8080,
        runtime="static",
        image="img",
        base_path=f"/sandbox/{task_id}/",
        started_at=now,
        last_request_at=now,
        status="running",
    )


@pytest.mark.asyncio
async def test_double_provision_yields_one_actual_build(tmp_path, monkeypatch):
    """Two coroutines call provision_idempotent for the same task at
    once. Only one should reach _raw_provision; the other returns the
    record the first inserted (or returns it cached after the lock
    releases)."""
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    svc = LifecycleService()

    call_count = 0
    record_holder: dict[str, SandboxRecord] = {}

    async def fake_provision(*, task_id, **_kwargs):
        nonlocal call_count
        call_count += 1
        # Simulate work duration so the second caller has to wait
        await asyncio.sleep(0.05)
        rec = _running_record(task_id)
        await registry.add(rec)
        record_holder[task_id] = rec
        return rec

    monkeypatch.setattr("sandbox_host.lifecycle._raw_provision", fake_provision)
    # We don't want the probe to mistakenly think the second caller
    # has a healthy server before the first one is even done; force
    # probe to False unless caller registers it themselves.
    monkeypatch.setattr(
        "sandbox_host.lifecycle._probe_tcp",
        AsyncMock(return_value=True),
    )

    async def call() -> SandboxRecord:
        return await svc.provision_idempotent(
            task_id="dup",
            workspace=tmp_path,
            spec=None,           # type: ignore[arg-type]
            settings=None,       # type: ignore[arg-type]
            registry=registry,
        )

    r1, r2 = await asyncio.gather(call(), call())
    assert call_count == 1, f"expected exactly one underlying provision, got {call_count}"
    assert r1.task_id == r2.task_id == "dup"


@pytest.mark.asyncio
async def test_second_provision_with_healthy_row_reuses(tmp_path, monkeypatch):
    """A new provision call when the row already says running and
    the TCP probe succeeds returns the existing record."""
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    svc = LifecycleService()
    pre_existing = _running_record("warm")
    await registry.add(pre_existing)

    fake_provision = AsyncMock()  # should NOT be called
    monkeypatch.setattr("sandbox_host.lifecycle._raw_provision", fake_provision)
    monkeypatch.setattr(
        "sandbox_host.lifecycle._probe_tcp", AsyncMock(return_value=True)
    )

    out = await svc.provision_idempotent(
        task_id="warm",
        workspace=tmp_path,
        spec=None,           # type: ignore[arg-type]
        settings=None,       # type: ignore[arg-type]
        registry=registry,
    )
    assert out.task_id == "warm"
    fake_provision.assert_not_called()


@pytest.mark.asyncio
async def test_second_provision_with_dead_row_tears_down_and_rebuilds(
    tmp_path, monkeypatch,
):
    """Existing row says running but probe fails → teardown then
    rebuild. teardown should be called once, _raw_provision once."""
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    svc = LifecycleService()
    stale = _running_record("dead")
    await registry.add(stale)

    teardown_calls: list[str] = []

    async def fake_teardown(*, task_id, **_kwargs):
        teardown_calls.append(task_id)
        await registry.remove(task_id)
        return True

    async def fake_provision(*, task_id, **_kwargs):
        rec = _running_record(task_id)
        rec.host_port = 99999  # distinguishable from `stale`
        await registry.add(rec)
        return rec

    monkeypatch.setattr("sandbox_host.lifecycle._raw_teardown", fake_teardown)
    monkeypatch.setattr("sandbox_host.lifecycle._raw_provision", fake_provision)
    monkeypatch.setattr(
        "sandbox_host.lifecycle._probe_tcp", AsyncMock(return_value=False)
    )

    out = await svc.provision_idempotent(
        task_id="dead",
        workspace=tmp_path,
        spec=None,           # type: ignore[arg-type]
        settings=None,       # type: ignore[arg-type]
        registry=registry,
    )
    assert teardown_calls == ["dead"]
    assert out.host_port == 99999  # the rebuilt one


@pytest.mark.asyncio
async def test_teardown_marks_row_stopped_keeping_generation(tmp_path, monkeypatch):
    """Phase 4 invariant: teardown leaves a 'stopped' row behind so
    the next provision can read prev.generation and bump to N+1."""
    registry = SandboxRegistry(db_path=tmp_path / "state.db")
    svc = LifecycleService()
    # Seed a running row at generation 7.
    pre = _running_record("gen7")
    pre.generation = 7
    await registry.add(pre)

    async def fake_teardown(*, task_id, registry=None, **_kwargs):
        # _raw_teardown's contract is to remove the row. Mimic that.
        if registry is not None:
            await registry.remove(task_id)
        return True

    monkeypatch.setattr("sandbox_host.lifecycle._raw_teardown", fake_teardown)

    await svc.teardown_idempotent(
        task_id="gen7",
        settings=None,       # type: ignore[arg-type]
        registry=registry,
    )
    fetched = await registry.get("gen7")
    assert fetched is not None
    assert fetched.status == "stopped"
    assert fetched.generation == 7
