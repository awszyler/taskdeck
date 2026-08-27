from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID  # noqa: TCH003 — Pydantic resolves this type at runtime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TCH002

from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import Task, TaskArtifact

if TYPE_CHECKING:
    from taskdeck_core.settings import Settings

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ArtifactOut(BaseModel):
    id: UUID
    task_id: UUID
    kind: str
    ref: str
    size_bytes: int


def _require_runner_token(authorization: str | None, settings: Settings) -> None:
    expected = f"Bearer {settings.runner_bearer_token}"
    if authorization != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid runner token")


@router.post(
    "/artifacts",
    status_code=status.HTTP_201_CREATED,
    response_model=ArtifactOut,
)
async def upload_artifact(
    request: Request,
    session: SessionDep,
    x_task_id: Annotated[UUID, Header()] = ...,  # type: ignore[assignment]
    x_artifact_kind: Annotated[str, Header()] = ...,  # type: ignore[assignment]
    x_artifact_meta: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> ArtifactOut:
    settings: Settings = request.app.state.settings
    _require_runner_token(authorization, settings)

    data = await request.body()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty body")

    task = await session.get(Task, x_task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")

    meta: dict = {}
    if x_artifact_meta:
        try:
            meta = json.loads(x_artifact_meta)
        except (json.JSONDecodeError, ValueError):
            meta = {}

    # Write through the artifact store (stored on app.state).
    store = request.app.state.artifact_store
    key = f"{x_task_id}/{x_artifact_kind}"
    size = await store.put(key, data)

    artifact = TaskArtifact(
        task_id=x_task_id,
        kind=x_artifact_kind,
        ref=key,
        meta=meta,
        size_bytes=size,
        created_at=datetime.now(UTC),
    )
    session.add(artifact)
    await session.commit()
    await session.refresh(artifact)

    return ArtifactOut(
        id=artifact.id,
        task_id=artifact.task_id,
        kind=artifact.kind,
        ref=artifact.ref,
        size_bytes=artifact.size_bytes or size,
    )
