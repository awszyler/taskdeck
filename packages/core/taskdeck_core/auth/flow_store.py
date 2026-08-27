"""In-memory store for the SRP / MFA roundtrip state.

Each login round-trips through the backend several times (init →
respond → maybe TOTP / MFA setup / new password). We can't put the
opaque ``Session`` string Cognito returns into a cookie or send it back
to the browser — it is itself an authentication credential that, if
intercepted, can be replayed without the password. So we hold it on
the backend and hand the browser a short-lived ``flow_id``.

V1 ships with an in-process implementation. ``FlowStore`` is a Protocol
so a Redis (or any external) implementation can drop in later when we
multi-worker the core service.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

log = logging.getLogger(__name__)


class FlowStore(Protocol):
    async def put(self, flow_id: str, state: dict[str, Any], ttl_seconds: int = 300) -> None: ...
    async def get(self, flow_id: str) -> dict[str, Any] | None: ...
    async def delete(self, flow_id: str) -> None: ...


@dataclass
class _Entry:
    state: dict[str, Any]
    expires_at: float = field(default_factory=lambda: time.monotonic() + 300)


class InMemoryFlowStore:
    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def new_flow_id() -> str:
        return uuid4().hex

    async def put(
        self, flow_id: str, state: dict[str, Any], ttl_seconds: int = 300
    ) -> None:
        async with self._lock:
            self._data[flow_id] = _Entry(
                state=state, expires_at=time.monotonic() + ttl_seconds
            )

    async def get(self, flow_id: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._data.get(flow_id)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._data.pop(flow_id, None)
                return None
            return entry.state

    async def delete(self, flow_id: str) -> None:
        async with self._lock:
            self._data.pop(flow_id, None)

    async def gc_loop(self, interval_seconds: int = 60) -> None:
        """Periodic janitor — remove expired entries.

        Started by ``main.lifespan`` when ``auth_mode=cognito``. Runs
        forever; cancelled on shutdown.
        """
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                async with self._lock:
                    now = time.monotonic()
                    stale = [
                        fid for fid, entry in self._data.items() if entry.expires_at <= now
                    ]
                    for fid in stale:
                        self._data.pop(fid, None)
                    if stale:
                        log.debug("flow_store gc: dropped %d expired entries", len(stale))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # Never let GC kill the loop — log and keep going.
                log.exception("flow_store gc iteration failed")
