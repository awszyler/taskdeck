from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from taskdeck_core.db.models import CostEvent
from taskdeck_core.metrics.registry import COST_EVENTS_TOTAL

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from taskdeck_core.cost.pricing import Pricing

log = logging.getLogger(__name__)


class CostEventSink:
    def __init__(self, *, sessionmaker: async_sessionmaker, pricing: Pricing, enabled: bool):
        self._sm = sessionmaker
        self._pricing = pricing
        self._enabled = enabled

    async def handle(self, event: dict) -> None:
        if not self._enabled:
            return
        if event.get("type") != "cost.event":
            return

        provider = event.get("provider", "")
        operation = event.get("operation", "")
        model = event.get("model")
        tokens_in = int(event.get("tokens_in") or 0)
        tokens_out = int(event.get("tokens_out") or 0)
        audio_seconds = event.get("audio_seconds")

        cost = None
        if audio_seconds is not None and model:
            cost = self._pricing.compute_audio(model, float(audio_seconds))
        elif model:
            cost = self._pricing.compute_tokens(model, tokens_in, tokens_out)

        try:
            workspace_id = UUID(event["workspace_id"]) if event.get("workspace_id") else None
            task_id = UUID(event["task_id"]) if event.get("task_id") else None
            user_id = UUID(event["user_id"]) if event.get("user_id") else None
        except (ValueError, TypeError):
            log.warning("cost.event had invalid UUID fields; dropping")
            return

        async with self._sm() as db:
            db.add(
                CostEvent(
                    workspace_id=workspace_id,
                    task_id=task_id,
                    user_id=user_id,
                    provider=provider,
                    operation=operation,
                    model=model,
                    tokens_in=tokens_in or None,
                    tokens_out=tokens_out or None,
                    cost_usd=cost,
                    meta=event.get("meta", {}),
                    created_at=datetime.now(UTC),
                )
            )
            await db.commit()

        COST_EVENTS_TOTAL.labels(provider=provider or "unknown", model=model or "unknown").inc()
