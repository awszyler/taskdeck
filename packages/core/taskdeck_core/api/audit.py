from __future__ import annotations

from typing import Annotated
from uuid import UUID  # noqa: TCH003 — FastAPI resolves this at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from taskdeck_core.auth.middleware import ServicePrincipal, current_principal
from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import AuditEvent, WorkspaceMember

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[object, Depends(current_principal)]


class AuditEventOut(BaseModel):
    id: str
    workspace_id: str | None
    user_id: str | None
    kind: str
    target_type: str | None
    target_id: str | None
    meta: dict
    created_at: str


class AuditEventsOut(BaseModel):
    items: list[AuditEventOut]


async def _require_workspace_member(
    session: AsyncSession,
    workspace_id: UUID,
    principal: object,
) -> None:
    if isinstance(principal, ServicePrincipal):
        return
    member = await session.get(WorkspaceMember, (workspace_id, principal.id))  # type: ignore[union-attr]
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this workspace")


@router.get("", response_model=AuditEventsOut)
async def list_audit_events(
    workspace_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> AuditEventsOut:
    await _require_workspace_member(session, workspace_id, principal)

    stmt = (
        select(AuditEvent)
        .where(AuditEvent.workspace_id == workspace_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(AuditEvent.kind == kind)
    if cursor:
        stmt = stmt.where(AuditEvent.created_at < cursor)

    rows = (await session.scalars(stmt)).all()
    return AuditEventsOut(
        items=[
            AuditEventOut(
                id=str(r.id),
                workspace_id=str(r.workspace_id) if r.workspace_id else None,
                user_id=str(r.user_id) if r.user_id else None,
                kind=r.kind,
                target_type=r.target_type,
                target_id=str(r.target_id) if r.target_id else None,
                meta=r.meta or {},
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ]
    )
