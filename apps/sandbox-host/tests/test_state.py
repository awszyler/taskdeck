"""SQLite-backed SandboxRegistry tests (P-H Phase 1).

Cover the contract every other module relies on: insert/get/list/touch/
remove + persistence across "restart" + write serialisation under
concurrent producers. These run cheaply (no docker, no network).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sandbox_host.state import SandboxRecord, SandboxRegistry


def _record(task_id: str = "t1", **overrides) -> SandboxRecord:
    now = datetime.now(UTC)
    base = dict(
        task_id=task_id,
        container_id=f"cid-{task_id}",
        container_name=f"td-sandbox-{task_id}",
        network_name=f"td-sandbox-net-{task_id}",
        host_port=12345,
        internal_port=8080,
        runtime="static",
        image="td-sandbox-static:latest",
        base_path=f"/sandbox/{task_id}/",
        started_at=now,
        last_request_at=now,
    )
    base.update(overrides)
    return SandboxRecord(**base)


@pytest.mark.asyncio
async def test_add_then_get_round_trips_all_fields(tmp_path):
    reg = SandboxRegistry(db_path=tmp_path / "state.db")
    rec = _record("alpha", status="running", error_message="boom")
    await reg.add(rec)
    fetched = await reg.get("alpha")
    assert fetched is not None
    # Compare every field that survives the SQLite round-trip.
    assert fetched.task_id == "alpha"
    assert fetched.container_id == rec.container_id
    assert fetched.container_name == rec.container_name
    assert fetched.network_name == rec.network_name
    assert fetched.host_port == rec.host_port
    assert fetched.internal_port == rec.internal_port
    assert fetched.runtime == rec.runtime
    assert fetched.image == rec.image
    assert fetched.base_path == rec.base_path
    assert fetched.started_at == rec.started_at
    assert fetched.last_request_at == rec.last_request_at
    assert fetched.status == "running"
    assert fetched.error_message == "boom"
    assert fetched.generation == 1


@pytest.mark.asyncio
async def test_get_returns_none_for_missing(tmp_path):
    reg = SandboxRegistry(db_path=tmp_path / "state.db")
    assert await reg.get("nope") is None


@pytest.mark.asyncio
async def test_state_persists_across_registry_restart(tmp_path):
    """The whole point of Phase 1: surviving a sandbox-host restart."""
    db = tmp_path / "state.db"
    reg1 = SandboxRegistry(db_path=db)
    await reg1.add(_record("survivor"))

    # Simulate restart: build a fresh SandboxRegistry on the same file.
    reg2 = SandboxRegistry(db_path=db)
    fetched = await reg2.get("survivor")
    assert fetched is not None
    assert fetched.task_id == "survivor"

    # list_all also reflects pre-restart state.
    rows = await reg2.list_all()
    assert {r.task_id for r in rows} == {"survivor"}


@pytest.mark.asyncio
async def test_touch_updates_last_request_at_durably(tmp_path):
    db = tmp_path / "state.db"
    reg = SandboxRegistry(db_path=db)
    old = datetime.now(UTC) - timedelta(hours=1)
    await reg.add(_record("hot", last_request_at=old))

    await reg.touch("hot")
    fetched = await reg.get("hot")
    assert fetched is not None
    assert fetched.last_request_at > old

    # Persists across a restart too.
    reg2 = SandboxRegistry(db_path=db)
    fetched2 = await reg2.get("hot")
    assert fetched2 is not None
    assert fetched2.last_request_at == fetched.last_request_at


@pytest.mark.asyncio
async def test_remove_returns_record_and_then_get_returns_none(tmp_path):
    reg = SandboxRegistry(db_path=tmp_path / "state.db")
    rec = _record("to-go")
    await reg.add(rec)
    removed = await reg.remove("to-go")
    assert removed is not None
    assert removed.task_id == "to-go"
    assert await reg.get("to-go") is None
    # Removing again is a no-op (idempotent), no exception.
    assert await reg.remove("to-go") is None


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_corrupt(tmp_path):
    """20 concurrent add() calls on distinct task_ids should all
    land cleanly. The asyncio.Lock + SQLite BEGIN IMMEDIATE means we
    never see "database is locked" propagating up to callers."""
    reg = SandboxRegistry(db_path=tmp_path / "state.db")
    n = 20

    async def writer(i: int) -> None:
        await reg.add(_record(f"task-{i:02d}"))

    await asyncio.gather(*(writer(i) for i in range(n)))
    rows = await reg.list_all()
    assert {r.task_id for r in rows} == {f"task-{i:02d}" for i in range(n)}


@pytest.mark.asyncio
async def test_add_upserts_on_same_task_id(tmp_path):
    """Provision retry / generation bump should overwrite the row,
    not raise on the PK conflict."""
    reg = SandboxRegistry(db_path=tmp_path / "state.db")
    await reg.add(_record("dup", host_port=10001))
    await reg.add(_record("dup", host_port=10002, status="error"))

    fetched = await reg.get("dup")
    assert fetched is not None
    assert fetched.host_port == 10002
    assert fetched.status == "error"
    # Only one row.
    rows = await reg.list_all()
    assert len(rows) == 1
