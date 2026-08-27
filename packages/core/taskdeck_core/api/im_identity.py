from __future__ import annotations

from typing import Annotated
from uuid import UUID  # noqa: TCH003

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import ImIdentityLink, Workspace

router = APIRouter(prefix="/api/v1/im", tags=["im"])


class BindCodeBody(BaseModel):
    workspace_id: UUID


class BindCodeOut(BaseModel):
    code: str
    expires_at: float


class IdentityLinkOut(BaseModel):
    id: UUID
    workspace_id: UUID
    platform: str
    external_id: str


class IdentityLinkListOut(BaseModel):
    items: list[IdentityLinkOut]


SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("/wecom/bind-code", response_model=BindCodeOut, status_code=status.HTTP_201_CREATED)
async def issue_bind_code(body: BindCodeBody, request: Request, session: SessionDep) -> BindCodeOut:
    ws = await session.get(Workspace, body.workspace_id)
    if ws is None:
        raise HTTPException(404, "workspace not found")
    cache = request.app.state.wecom_bind_codes
    code, expires_at = cache.issue(workspace_id=body.workspace_id)
    return BindCodeOut(code=code, expires_at=expires_at)


@router.get("/identity-links", response_model=IdentityLinkListOut)
async def list_identity_links(session: SessionDep) -> IdentityLinkListOut:
    stmt = select(ImIdentityLink).order_by(ImIdentityLink.created_at.desc())
    rows = (await session.scalars(stmt)).all()
    return IdentityLinkListOut(
        items=[
            IdentityLinkOut(id=r.id, workspace_id=r.workspace_id, platform=r.platform, external_id=r.external_id)
            for r in rows
        ]
    )


@router.delete("/identity-links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity_link(link_id: UUID, session: SessionDep) -> None:
    link = await session.get(ImIdentityLink, link_id)
    if link is None:
        raise HTTPException(404, "link not found")
    await session.delete(link)
    await session.commit()
