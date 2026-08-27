from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID  # noqa: TCH003  — Pydantic resolves this at runtime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from taskdeck_core.auth.memberships import get_visible_workspace_ids
from taskdeck_core.auth.middleware import ServicePrincipal, current_principal, require_user
from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import User, Workspace, WorkspaceMember

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


class WorkspaceCreateBody(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=128)


class WorkspaceOut(BaseModel):
    id: UUID
    slug: str
    name: str
    created_at: datetime

    @classmethod
    def from_model(cls, w: Workspace) -> WorkspaceOut:
        return cls(id=w.id, slug=w.slug, name=w.name, created_at=w.created_at)


class WorkspaceListOut(BaseModel):
    items: list[WorkspaceOut]


SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[User | ServicePrincipal, Depends(current_principal)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=WorkspaceOut)
async def create_workspace(
    body: WorkspaceCreateBody,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> WorkspaceOut:
    settings = request.app.state.settings if hasattr(request.app.state, "settings") else None

    # In github auth mode, only real users may create workspaces.
    if settings is not None and settings.auth_mode != "disabled":
        user: User | None = require_user(principal)
    else:
        user = None  # legacy single-user mode

    ws = Workspace(slug=body.slug, name=body.name)
    session.add(ws)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"slug '{body.slug}' already exists") from None

    # Auto-add the creating user as owner when auth is enabled.
    if settings is not None and settings.auth_mode != "disabled" and user is not None:
        session.add(
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=user.id,
                role="owner",
                created_at=datetime.now(UTC),
            )
        )

    await session.commit()
    await session.refresh(ws)

    bus = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        await bus.publish({
            "type": "audit.event",
            "kind": "workspace.create",
            "user_id": str(user.id) if user is not None else None,
            "workspace_id": str(ws.id),
            "target_type": "workspace",
            "target_id": str(ws.id),
            "meta": {"slug": ws.slug, "name": ws.name},
        })

    return WorkspaceOut.from_model(ws)


@router.get("", response_model=WorkspaceListOut)
async def list_workspaces(
    session: SessionDep,
    principal: PrincipalDep,
) -> WorkspaceListOut:
    visible = await get_visible_workspace_ids(session, principal)
    stmt = select(Workspace).order_by(Workspace.created_at.asc())
    if visible is not None:
        stmt = stmt.where(Workspace.id.in_(visible))
    rows = (await session.scalars(stmt)).all()
    return WorkspaceListOut(items=[WorkspaceOut.from_model(w) for w in rows])
