from __future__ import annotations

from typing import Annotated
from uuid import UUID  # noqa: TC003

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from taskdeck_core.auth.memberships import get_visible_workspace_ids
from taskdeck_core.auth.middleware import ServicePrincipal, current_principal
from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import MemoryChunk, User

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


class MemoryChunkOut(BaseModel):
    id: UUID
    workspace_id: UUID
    source_kind: str
    source_task_id: UUID | None
    source_artifact_id: UUID | None
    text: str
    meta: dict
    created_at: str


class MemoryChunkListOut(BaseModel):
    items: list[MemoryChunkOut]


class MemoryAddBody(BaseModel):
    workspace_id: UUID
    text: str = Field(min_length=1, max_length=8192)
    source_kind: str = Field(default="manual", max_length=32)
    meta: dict = Field(default_factory=dict)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
# `User | ServicePrincipal` matches current_principal's return; pyright wants
# the precise union before `get_visible_workspace_ids(...)` accepts it.
PrincipalDep = Annotated[User | ServicePrincipal, Depends(current_principal)]


@router.get("", response_model=MemoryChunkListOut)
async def list_memory(
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    workspace_id: UUID = Query(...),  # noqa: B008
    q: str | None = Query(None, description="If provided, ranks results by cosine similarity to this text."),  # noqa: B008
    limit: int = Query(50, ge=1, le=200),  # noqa: B008
) -> MemoryChunkListOut:
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and workspace_id not in visible:
        raise HTTPException(404, "workspace not visible")

    if q:
        # Embed the query and rank by cosine distance.
        embedding_client = getattr(request.app.state, "embedding_client", None)
        if embedding_client is None:
            raise HTTPException(503, "embedding client not configured")
        try:
            vec = (await embedding_client.embed_batch([q], input_type="search_query"))[0]
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"embedding failed: {e}") from e
        stmt = (
            select(MemoryChunk)
            .where(MemoryChunk.workspace_id == workspace_id)
            .order_by(MemoryChunk.embedding.cosine_distance(vec))
            .limit(limit)
        )
    else:
        stmt = (
            select(MemoryChunk)
            .where(MemoryChunk.workspace_id == workspace_id)
            .order_by(MemoryChunk.created_at.desc())
            .limit(limit)
        )
    rows = (await session.scalars(stmt)).all()
    return MemoryChunkListOut(
        items=[
            MemoryChunkOut(
                id=r.id,
                workspace_id=r.workspace_id,
                source_kind=r.source_kind,
                source_task_id=r.source_task_id,
                source_artifact_id=r.source_artifact_id,
                text=r.text,
                meta=r.meta or {},
                created_at=r.created_at.isoformat() if r.created_at else "",
            )
            for r in rows
        ]
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=MemoryChunkOut)
async def add_memory(
    body: MemoryAddBody,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
) -> MemoryChunkOut:
    from datetime import UTC, datetime

    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and body.workspace_id not in visible:
        raise HTTPException(404, "workspace not visible")

    embedding_client = getattr(request.app.state, "embedding_client", None)
    embedding = None
    if embedding_client is not None:
        try:
            embedding = (await embedding_client.embed_batch([body.text]))[0]
        except Exception:  # noqa: BLE001
            embedding = None  # store the chunk anyway, just without the vector

    chunk = MemoryChunk(
        workspace_id=body.workspace_id,
        source_kind=body.source_kind,
        text=body.text,
        embedding=embedding,
        meta=body.meta,
        created_at=datetime.now(UTC),
    )
    session.add(chunk)
    await session.commit()
    await session.refresh(chunk)
    return MemoryChunkOut(
        id=chunk.id,
        workspace_id=chunk.workspace_id,
        source_kind=chunk.source_kind,
        source_task_id=chunk.source_task_id,
        source_artifact_id=chunk.source_artifact_id,
        text=chunk.text,
        meta=chunk.meta or {},
        created_at=chunk.created_at.isoformat(),
    )


@router.delete("/{chunk_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    chunk_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> None:
    chunk = await session.get(MemoryChunk, chunk_id)
    if chunk is None:
        raise HTTPException(404, "chunk not found")
    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and chunk.workspace_id not in visible:
        raise HTTPException(404, "chunk not found")
    await session.delete(chunk)
    await session.commit()
