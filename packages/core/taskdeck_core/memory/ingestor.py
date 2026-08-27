from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from taskdeck_core.db.models import MemoryChunk, Task, TaskArtifact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from taskdeck_core.memory.embedding import EmbeddingClient

log = logging.getLogger(__name__)

_DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.MULTILINE)
_MAX_HUNK_BYTES = 2048


def _split_into_hunks(text: str, max_bytes: int = _MAX_HUNK_BYTES) -> list[str]:
    """Split a git diff into per-file chunks, then sub-split on blank lines if too large."""
    file_chunks = _DIFF_SPLIT_RE.split(text)
    result: list[str] = []
    for fc in file_chunks:
        fc = fc.strip()
        if not fc:
            continue
        if len(fc.encode()) <= max_bytes:
            result.append(fc)
        else:
            # Sub-split on blank lines
            parts = re.split(r"\n\n+", fc)
            current: list[str] = []
            current_size = 0
            for part in parts:
                part_size = len(part.encode())
                if current and current_size + part_size > max_bytes:
                    result.append("\n\n".join(current))
                    current = [part]
                    current_size = part_size
                else:
                    current.append(part)
                    current_size += part_size
            if current:
                result.append("\n\n".join(current))
    return result or [text[:max_bytes]]


class MemoryIngestor:
    """Subscribes to EventBus; on task.event(to=done), embeds and stores memory chunks."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker,
        artifact_store,
        embedding_client: EmbeddingClient,
        enabled: bool,
    ) -> None:
        self._sm = sessionmaker
        self._art = artifact_store
        self._embed = embedding_client
        self._enabled = enabled

    async def handle(self, event: dict) -> None:
        if not self._enabled:
            return
        if event.get("type") != "task.event":
            return
        if event.get("to") != "done":
            return

        task_id_raw = event.get("task_id")
        if not task_id_raw:
            return
        try:
            task_id = UUID(str(task_id_raw))
        except (ValueError, TypeError):
            log.warning("memory ingestor: invalid task_id %r", task_id_raw)
            return

        try:
            await self._ingest(task_id)
        except Exception:
            log.exception("memory ingestor: failed to ingest task %s", task_id)

    async def _ingest(self, task_id: UUID) -> None:
        async with self._sm() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return

            chunks: list[tuple[str, str, UUID | None, dict]] = []
            # (source_kind, text, source_artifact_id, meta)

            if task.summary:
                chunks.append(
                    ("task-summary", task.summary, None, {"task_id": str(task.id)})
                )

            artifacts = (
                await db.scalars(
                    select(TaskArtifact).where(TaskArtifact.task_id == task.id)
                )
            ).all()

            for art in artifacts:
                if art.kind not in ("decision", "git-diff"):
                    continue
                try:
                    raw = await self._art.read(art.ref)
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    log.warning("memory ingestor: could not read artifact %s", art.id)
                    continue

                if art.kind == "decision":
                    chunks.append(
                        ("artifact-decision", text, art.id, {"artifact_id": str(art.id)})
                    )
                else:
                    for hunk in _split_into_hunks(text):
                        chunks.append(
                            (
                                "artifact-git-diff",
                                hunk,
                                art.id,
                                {"artifact_id": str(art.id)},
                            )
                        )

            if not chunks:
                return

            texts = [c[1] for c in chunks]
            vecs = await self._embed.embed_batch(texts)

            now = datetime.now(UTC)
            for (kind, text, art_id, meta), vec in zip(chunks, vecs, strict=True):
                db.add(
                    MemoryChunk(
                        workspace_id=task.workspace_id,
                        source_kind=kind,
                        source_task_id=task.id,
                        source_artifact_id=art_id,
                        text=text,
                        embedding=vec,
                        meta=meta,
                        created_at=now,
                    )
                )
            await db.commit()
            log.info(
                "memory ingestor: stored %d chunk(s) for task %s", len(chunks), task_id
            )
