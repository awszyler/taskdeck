from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param

from taskdeck_core.auth.session import (
    CognitoRefreshFailed,
    refresh_session,
)
from taskdeck_core.db.models import User, UserSession


@dataclass
class ServicePrincipal:
    kind: str  # "legacy_single_user" | "service_token"


REFRESH_LEEWAY = timedelta(seconds=30)


async def current_principal(request: Request) -> User | ServicePrincipal:
    settings = getattr(request.app.state, "settings", None)

    # 1. Service principal (runner bearer) — recognised in any mode so
    #    runner ↔ core works regardless of auth_mode.
    auth_header = request.headers.get("authorization")
    scheme, token = get_authorization_scheme_param(auth_header or "")
    if scheme.lower() == "bearer" and token:
        if settings is not None and token == settings.runner_bearer_token:
            return ServicePrincipal(kind="service_token")
        # In disabled mode we accept *anything* on the bearer path silently
        # (legacy single-user). In cognito mode we treat a wrong runner
        # bearer as a hard 401 — bearer is reserved for runners.
        if settings is None or settings.auth_mode == "disabled":
            return ServicePrincipal(kind="legacy_single_user")
        raise HTTPException(401, "invalid bearer token")

    if settings is None or settings.auth_mode == "disabled":
        return ServicePrincipal(kind="legacy_single_user")

    # 2. Cognito mode — opaque session cookie
    cookie_name = settings.session_cookie_name
    sid = request.cookies.get(cookie_name)
    if not sid:
        raise HTTPException(401, "no session")
    try:
        session_id = UUID(sid)
    except ValueError:
        raise HTTPException(401, "invalid session id") from None

    fernet = request.app.state.fernet
    cognito = request.app.state.cognito_client
    sm = request.app.state.db_sessionmaker

    async with sm() as db:
        sess_row = await db.get(UserSession, session_id)
        if sess_row is None:
            raise HTTPException(401, "session not found")

        now = datetime.now(UTC)
        if _utc(sess_row.refresh_token_expires_at) <= now:
            await db.delete(sess_row)
            await db.commit()
            raise HTTPException(401, "session expired")

        if _utc(sess_row.access_token_expires_at) <= now + REFRESH_LEEWAY:
            try:
                await refresh_session(
                    db, sess_row=sess_row, fernet=fernet, cognito=cognito
                )
            except CognitoRefreshFailed:
                await db.delete(sess_row)
                await db.commit()
                raise HTTPException(401, "session refresh failed") from None

        sess_row.last_seen_at = now
        user = await db.get(User, sess_row.user_id)
        await db.commit()

    if user is None:
        raise HTTPException(401, "user not found")
    return user


def require_user(principal: User | ServicePrincipal) -> User:
    if not isinstance(principal, User):
        raise HTTPException(403, "user authentication required")
    return principal


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
