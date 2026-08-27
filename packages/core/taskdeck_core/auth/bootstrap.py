from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from taskdeck_core.auth.middleware import ServicePrincipal, current_principal, require_user
from taskdeck_core.db.models import User, Workspace, WorkspaceMember

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

PrincipalDep = Annotated[User | ServicePrincipal, Depends(current_principal)]


@router.post("/bootstrap-ownership")
async def bootstrap_ownership(
    request: Request,
    principal: PrincipalDep,
) -> dict:
    """If no workspace_members rows exist, the first authenticated caller
    becomes owner of ALL existing workspaces. This is a one-time bootstrap
    mechanism after flipping auth_mode from disabled to cognito."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None or settings.auth_mode == "disabled":
        raise HTTPException(400, "auth disabled")

    user = require_user(principal)

    sm = request.app.state.db_sessionmaker
    async with sm() as db:
        count = (
            await db.execute(select(func.count()).select_from(WorkspaceMember))
        ).scalar_one()
        if count > 0:
            raise HTTPException(400, "bootstrap already completed")

        stmt = select(Workspace)
        workspaces = (await db.scalars(stmt)).all()
        now = datetime.now(UTC)
        for w in workspaces:
            db.add(
                WorkspaceMember(
                    workspace_id=w.id,
                    user_id=user.id,
                    role="owner",
                    created_at=now,
                )
            )
        await db.commit()

    return {"claimed": len(workspaces)}
