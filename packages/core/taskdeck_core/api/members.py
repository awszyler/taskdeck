from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID  # noqa: TCH003

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from taskdeck_core.auth.middleware import ServicePrincipal, current_principal, require_user
from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import User, Workspace, WorkspaceInvite, WorkspaceMember

router = APIRouter(prefix="/api/v1/workspaces", tags=["members"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[User | ServicePrincipal, Depends(current_principal)]

INVITE_TTL_HOURS = 24


class InviteOut(BaseModel):
    code: str
    expires_at: datetime


class MemberOut(BaseModel):
    user_id: str
    role: str
    login: str | None
    avatar_url: str | None


class MemberListOut(BaseModel):
    items: list[MemberOut]


class JoinBody(BaseModel):
    code: str


async def _require_member(
    session: AsyncSession, workspace_id: UUID, user_id: UUID
) -> WorkspaceMember:
    member = await session.get(WorkspaceMember, (workspace_id, user_id))
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this workspace")
    return member


async def _require_owner(
    session: AsyncSession, workspace_id: UUID, user_id: UUID
) -> WorkspaceMember:
    member = await _require_member(session, workspace_id, user_id)
    if member.role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner role required")
    return member


@router.post("/{workspace_id}/invites", response_model=InviteOut)
async def create_invite(
    workspace_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> InviteOut:
    user = require_user(principal)

    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")

    await _require_owner(session, workspace_id, user.id)

    code = secrets.token_urlsafe(9)[:12]  # 12-char alphanumeric-ish code
    expires_at = datetime.now(UTC) + timedelta(hours=INVITE_TTL_HOURS)
    invite = WorkspaceInvite(
        code=code,
        workspace_id=workspace_id,
        created_by=user.id,
        expires_at=expires_at,
    )
    session.add(invite)
    await session.commit()

    bus = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        await bus.publish({
            "type": "audit.event",
            "kind": "invite.issue",
            "user_id": str(user.id),
            "workspace_id": str(workspace_id),
            "target_type": "invite",
            "meta": {"code_prefix": code[:4]},
        })

    return InviteOut(code=code, expires_at=expires_at)


@router.post("/join", status_code=status.HTTP_204_NO_CONTENT)
async def join_workspace(
    body: JoinBody,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> None:
    user = require_user(principal)

    invite = await session.get(WorkspaceInvite, body.code)
    if invite is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired invite code")
    if invite.consumed_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invite already used")
    if invite.expires_at < datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invite expired")

    workspace_id = invite.workspace_id

    # Idempotent — don't double-add.
    existing = await session.get(WorkspaceMember, (workspace_id, user.id))
    if existing is None:
        session.add(
            WorkspaceMember(
                workspace_id=workspace_id,
                user_id=user.id,
                role="member",
                created_at=datetime.now(UTC),
            )
        )

    invite.consumed_at = datetime.now(UTC)
    invite.consumed_by = user.id
    await session.commit()

    bus = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        await bus.publish({
            "type": "audit.event",
            "kind": "invite.consume",
            "user_id": str(user.id),
            "workspace_id": str(workspace_id),
            "target_type": "workspace",
            "target_id": str(workspace_id),
        })


@router.get("/{workspace_id}/members", response_model=MemberListOut)
async def list_members(
    workspace_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> MemberListOut:
    user = require_user(principal)

    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")

    # Only members of the workspace can list members.
    await _require_member(session, workspace_id, user.id)

    stmt = (
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
    )
    rows = (await session.execute(stmt)).all()
    items = [
        MemberOut(
            user_id=str(member.user_id),
            role=member.role,
            login=u.login,
            avatar_url=u.avatar_url,
        )
        for member, u in rows
    ]
    return MemberListOut(items=items)


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    workspace_id: UUID,
    user_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> None:
    caller = require_user(principal)

    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")

    await _require_owner(session, workspace_id, caller.id)

    # Prevent removing the last owner.
    target = await session.get(WorkspaceMember, (workspace_id, user_id))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")

    if target.role == "owner":
        owner_count = (
            await session.execute(
                select(func.count())
                .select_from(WorkspaceMember)
                .where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.role == "owner",
                )
            )
        ).scalar_one()
        if owner_count <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "cannot remove the last owner",
            )

    await session.delete(target)
    await session.commit()

    bus = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        await bus.publish({
            "type": "audit.event",
            "kind": "member.remove",
            "user_id": str(caller.id),
            "workspace_id": str(workspace_id),
            "target_type": "user",
            "target_id": str(user_id),
        })
