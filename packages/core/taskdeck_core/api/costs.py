from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID  # noqa: TCH003 — FastAPI resolves this at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from taskdeck_core.auth.middleware import ServicePrincipal, current_principal
from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import CostEvent, WorkspaceMember

router = APIRouter(prefix="/api/v1/costs", tags=["costs"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[object, Depends(current_principal)]


class DayEntry(BaseModel):
    date: str
    usd: str


class CostsSummaryOut(BaseModel):
    total_usd: str
    by_operation: dict[str, str]
    by_user: dict[str, str]
    by_day: list[DayEntry]


class CostEventOut(BaseModel):
    id: str
    workspace_id: str | None
    task_id: str | None
    user_id: str | None
    provider: str
    operation: str
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: str | None
    created_at: str


class CostEventsOut(BaseModel):
    items: list[CostEventOut]


async def _require_workspace_member(
    session: AsyncSession,
    workspace_id: UUID,
    principal: object,
) -> None:
    """Reject if the caller is not a member of the workspace.

    ServicePrincipal (runner token / auth-disabled) has blanket access.
    """
    if isinstance(principal, ServicePrincipal):
        return
    member = await session.get(WorkspaceMember, (workspace_id, principal.id))  # type: ignore[union-attr]
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this workspace")


@router.get("/summary", response_model=CostsSummaryOut)
async def costs_summary(
    workspace_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
) -> CostsSummaryOut:
    await _require_workspace_member(session, workspace_id, principal)

    base_filter = [CostEvent.workspace_id == workspace_id]
    if from_date:
        base_filter.append(CostEvent.created_at >= from_date)
    if to_date:
        base_filter.append(CostEvent.created_at <= to_date)

    # Total
    total_row = (
        await session.execute(
            select(func.sum(CostEvent.cost_usd)).where(*base_filter)
        )
    ).scalar_one()
    total_usd = total_row or Decimal("0")

    # By operation
    op_rows = (
        await session.execute(
            select(CostEvent.operation, func.sum(CostEvent.cost_usd))
            .where(*base_filter)
            .group_by(CostEvent.operation)
        )
    ).all()
    by_operation = {op: str(amt or Decimal("0")) for op, amt in op_rows}

    # By user
    user_rows = (
        await session.execute(
            select(CostEvent.user_id, func.sum(CostEvent.cost_usd))
            .where(*base_filter)
            .where(CostEvent.user_id.isnot(None))
            .group_by(CostEvent.user_id)
        )
    ).all()
    by_user = {str(uid): str(amt or Decimal("0")) for uid, amt in user_rows}

    # By day
    day_rows = (
        await session.execute(
            select(
                func.date_trunc("day", CostEvent.created_at).label("day"),
                func.sum(CostEvent.cost_usd),
            )
            .where(*base_filter)
            .group_by("day")
            .order_by("day")
        )
    ).all()
    by_day = [
        DayEntry(date=str(day.date()), usd=str(amt or Decimal("0")))
        for day, amt in day_rows
    ]

    return CostsSummaryOut(
        total_usd=str(total_usd),
        by_operation=by_operation,
        by_user=by_user,
        by_day=by_day,
    )


@router.get("/events", response_model=CostEventsOut)
async def costs_events(
    workspace_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> CostEventsOut:
    await _require_workspace_member(session, workspace_id, principal)

    stmt = (
        select(CostEvent)
        .where(CostEvent.workspace_id == workspace_id)
        .order_by(CostEvent.created_at.desc())
        .limit(limit)
    )
    if cursor:
        stmt = stmt.where(CostEvent.created_at < cursor)

    rows = (await session.scalars(stmt)).all()
    return CostEventsOut(
        items=[
            CostEventOut(
                id=str(r.id),
                workspace_id=str(r.workspace_id) if r.workspace_id else None,
                task_id=str(r.task_id) if r.task_id else None,
                user_id=str(r.user_id) if r.user_id else None,
                provider=r.provider,
                operation=r.operation,
                model=r.model,
                tokens_in=r.tokens_in,
                tokens_out=r.tokens_out,
                cost_usd=str(r.cost_usd) if r.cost_usd is not None else None,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ]
    )
