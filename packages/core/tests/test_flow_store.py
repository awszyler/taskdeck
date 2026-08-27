from __future__ import annotations

import asyncio

import pytest
from taskdeck_core.auth.flow_store import InMemoryFlowStore


@pytest.mark.asyncio
async def test_put_get_roundtrip() -> None:
    store = InMemoryFlowStore()
    fid = store.new_flow_id()
    await store.put(fid, {"step": "init"})
    state = await store.get(fid)
    assert state == {"step": "init"}


@pytest.mark.asyncio
async def test_get_after_delete_returns_none() -> None:
    store = InMemoryFlowStore()
    fid = store.new_flow_id()
    await store.put(fid, {"x": 1})
    await store.delete(fid)
    assert await store.get(fid) is None


@pytest.mark.asyncio
async def test_ttl_expiry() -> None:
    store = InMemoryFlowStore()
    fid = store.new_flow_id()
    await store.put(fid, {"x": 1}, ttl_seconds=0)
    # Yield once so monotonic clock advances past 0-ttl.
    await asyncio.sleep(0.01)
    assert await store.get(fid) is None


@pytest.mark.asyncio
async def test_concurrent_put_get_no_corruption() -> None:
    store = InMemoryFlowStore()

    async def worker(i: int) -> None:
        fid = store.new_flow_id()
        await store.put(fid, {"i": i})
        assert (await store.get(fid)) == {"i": i}

    await asyncio.gather(*(worker(i) for i in range(50)))
