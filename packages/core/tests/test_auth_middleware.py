from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from taskdeck_core.auth.middleware import ServicePrincipal, current_principal
from taskdeck_core.db.models import User
from taskdeck_core.settings import Settings


def _make_request(
    *,
    auth_mode: str = "disabled",
    runner_bearer_token: str = "runner-secret",
    headers: dict | None = None,
    cookies: dict | None = None,
    db_user: User | None = None,
    sess_row: object | None = None,
) -> MagicMock:
    settings = MagicMock(spec=Settings)
    settings.auth_mode = auth_mode
    settings.runner_bearer_token = runner_bearer_token
    settings.session_cookie_name = "ccpt_session"
    settings.session_cookie_domain = ""

    app = FastAPI()
    app.state.settings = settings

    async def _mock_get(model_cls, pk):
        from taskdeck_core.db.models import UserSession

        if model_cls is UserSession:
            return sess_row
        if model_cls is User:
            return db_user
        return None

    mock_session = AsyncMock()
    mock_session.get = _mock_get
    mock_session.commit = AsyncMock()
    mock_session.delete = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    sm = MagicMock()
    sm.return_value = mock_ctx
    app.state.db_sessionmaker = sm

    # Stub fernet + cognito so the cognito path can run if needed.
    app.state.fernet = Fernet(Fernet.generate_key())
    app.state.cognito_client = MagicMock()

    request = MagicMock()
    request.app = app
    request.headers = MagicMock()
    request.headers.get = lambda key, default=None: (headers or {}).get(key, default)
    request.cookies = dict(cookies or {})
    return request


def _stub_session_row(*, user_id, access_exp_offset_seconds=600, refresh_exp_days=29):
    """Build a minimal UserSession-like object the middleware can inspect."""
    now = datetime.now(UTC)
    f = Fernet(Fernet.generate_key())
    return MagicMock(
        id=uuid4(),
        user_id=user_id,
        cognito_sub="sub-x",
        encrypted_refresh_token=f.encrypt(b"r"),
        encrypted_access_token=f.encrypt(b"a"),
        access_token_expires_at=now + timedelta(seconds=access_exp_offset_seconds),
        refresh_token_expires_at=now + timedelta(days=refresh_exp_days),
        last_seen_at=now,
    )


# ── disabled mode ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_mode_no_headers_returns_service_principal() -> None:
    req = _make_request(auth_mode="disabled")
    out = await current_principal(req)
    assert isinstance(out, ServicePrincipal)
    assert out.kind == "legacy_single_user"


@pytest.mark.asyncio
async def test_disabled_mode_with_cookie_still_service_principal() -> None:
    req = _make_request(auth_mode="disabled", cookies={"ccpt_session": str(uuid4())})
    out = await current_principal(req)
    assert isinstance(out, ServicePrincipal)


@pytest.mark.asyncio
async def test_runner_bearer_returns_service_principal() -> None:
    req = _make_request(
        auth_mode="cognito",
        runner_bearer_token="runner-secret",
        headers={"authorization": "Bearer runner-secret"},
    )
    out = await current_principal(req)
    assert isinstance(out, ServicePrincipal)
    assert out.kind == "service_token"


# ── cognito mode rejects ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cognito_mode_no_cookie_raises_401() -> None:
    req = _make_request(auth_mode="cognito")
    with pytest.raises(HTTPException) as e:
        await current_principal(req)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_cognito_mode_invalid_uuid_raises_401() -> None:
    req = _make_request(auth_mode="cognito", cookies={"ccpt_session": "bogus"})
    with pytest.raises(HTTPException) as e:
        await current_principal(req)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_cognito_mode_session_not_found_raises_401() -> None:
    req = _make_request(
        auth_mode="cognito",
        cookies={"ccpt_session": str(uuid4())},
        sess_row=None,
    )
    with pytest.raises(HTTPException) as e:
        await current_principal(req)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_cognito_mode_invalid_runner_bearer_raises_401() -> None:
    req = _make_request(
        auth_mode="cognito",
        runner_bearer_token="runner-secret",
        headers={"authorization": "Bearer wrong-token"},
    )
    with pytest.raises(HTTPException) as e:
        await current_principal(req)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_cognito_mode_valid_session_returns_user() -> None:
    user = User(id=uuid4(), email="a@b.c", name="Alice", role="member")
    sess = _stub_session_row(user_id=user.id)
    req = _make_request(
        auth_mode="cognito",
        cookies={"ccpt_session": str(sess.id)},
        db_user=user,
        sess_row=sess,
    )
    out = await current_principal(req)
    assert isinstance(out, User)
    assert out.id == user.id
