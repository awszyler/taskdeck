from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TCH003 — used in set[UUID] return type

from sqlalchemy import select

from taskdeck_core.auth.middleware import ServicePrincipal
from taskdeck_core.db.models import User, WorkspaceMember

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def get_visible_workspace_ids(
    session: AsyncSession, principal: User | ServicePrincipal
) -> set[UUID] | None:
    """Return the set of workspace UUIDs the principal can see.

    None = see all (ServicePrincipal or auth_mode=disabled).
    Empty set = authenticated but no memberships yet.
    """
    if isinstance(principal, ServicePrincipal):
        return None
    stmt = select(WorkspaceMember.workspace_id).where(
        WorkspaceMember.user_id == principal.id
    )
    rows = (await session.scalars(stmt)).all()
    return set(rows)
