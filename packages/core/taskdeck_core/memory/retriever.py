from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TCH003

from sqlalchemy import select

from taskdeck_core.db.models import MemoryChunk

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from taskdeck_core.memory.embedding import EmbeddingClient

log = logging.getLogger(__name__)


async def retrieve(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    query_text: str,
    embedding_client: EmbeddingClient,
    top_k: int,
    per_cap: int,
    total_cap: int,
) -> list[dict]:
    """Return up to *top_k* memory chunks closest to *query_text*, budget-capped."""
    if not query_text.strip():
        return []

    vec = (await embedding_client.embed_batch([query_text], input_type="search_query"))[0]

    stmt = (
        select(MemoryChunk, MemoryChunk.embedding.cosine_distance(vec).label("score"))
        .where(MemoryChunk.workspace_id == workspace_id)
        .order_by("score")
        .limit(top_k)
    )
    rows = (await db.execute(stmt)).all()

    out: list[dict] = []
    remaining = total_cap
    for chunk, score in rows:
        text = chunk.text[:per_cap]
        if len(text) > remaining:
            text = text[: max(0, remaining - 20)] + "\n[...truncated]"
        remaining -= len(text)
        out.append(
            {
                "chunk_id": str(chunk.id),
                "source_kind": chunk.source_kind,
                "text": text,
                "score": float(score),
                "source_task_id": str(chunk.source_task_id) if chunk.source_task_id else None,
            }
        )
        if remaining <= 0:
            break

    return out
