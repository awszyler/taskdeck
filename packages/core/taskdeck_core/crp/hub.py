from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from taskdeck_core.metrics.registry import RUNNERS_CONNECTED


class _SocketLike(Protocol):
    async def send_json(self, data: dict) -> None: ...


@dataclass
class RunnerConnection:
    runner_id: str
    socket: _SocketLike
    max_parallel: int
    capabilities: list[str]
    capability_descriptions: dict[str, str] = field(default_factory=dict)
    _inflight: int = field(default=0, init=False)

    @property
    def inflight(self) -> int:
        return self._inflight

    def can_accept(self) -> bool:
        return self._inflight < self.max_parallel

    def increment_inflight(self) -> None:
        self._inflight += 1

    def decrement_inflight(self) -> None:
        if self._inflight > 0:
            self._inflight -= 1


class RunnerHub:
    """In-memory registry of connected runners.

    M1 scope: single-process, no distributed coordination.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, RunnerConnection] = {}

    def register(self, conn: RunnerConnection) -> None:
        is_new = conn.runner_id not in self._by_id
        self._by_id[conn.runner_id] = conn
        if is_new:
            RUNNERS_CONNECTED.inc()

    def unregister(self, runner_id: str) -> None:
        if self._by_id.pop(runner_id, None) is not None:
            RUNNERS_CONNECTED.dec()

    def pick_for(self, capability: str) -> RunnerConnection | None:
        candidates = [
            c for c in self._by_id.values()
            if capability in c.capabilities and c.can_accept()
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: c.inflight)

    def get(self, runner_id: str) -> RunnerConnection | None:
        return self._by_id.get(runner_id)

    def all_runners(self) -> list[RunnerConnection]:
        return list(self._by_id.values())

    def available_capabilities(self) -> list[dict[str, str]]:
        """Return a list of {capability, description} for every distinct capability
        across connected runners. Deduplicated so two runners offering "claude-code"
        appear once. Empty descriptions are kept (parser handles them)."""
        seen: dict[str, str] = {}
        for conn in self._by_id.values():
            for cap in conn.capabilities:
                if cap in seen:
                    continue
                seen[cap] = conn.capability_descriptions.get(cap, "")
        return [{"capability": cap, "description": desc} for cap, desc in seen.items()]
