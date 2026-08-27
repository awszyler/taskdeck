from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/runners", tags=["runners"])


class CapacityOut(BaseModel):
    """Aggregate capacity across all *currently connected* runners.

    Source: RunnerHub's in-memory registry — `inflight` is the number
    of tasks the dispatcher has handed to that runner and not yet seen
    finish; `max_parallel` is what the runner declared on register. We
    sum because the kanban Running column is workspace-agnostic and
    the user wants a single "are we wedged?" indicator.
    """
    running: int     # sum of inflight across connected runners
    capacity: int    # sum of max_parallel
    runners: int     # number of connected runners


@router.get("/capacity", response_model=CapacityOut)
async def get_capacity(request: Request) -> CapacityOut:
    hub = getattr(request.app.state, "runner_hub", None)
    if hub is None:
        # Bare/test app without a hub: degrade to zeroes rather than 500.
        return CapacityOut(running=0, capacity=0, runners=0)

    conns = hub.all_runners()
    running = sum(c.inflight for c in conns)
    capacity = sum(c.max_parallel for c in conns)
    return CapacityOut(running=running, capacity=capacity, runners=len(conns))
