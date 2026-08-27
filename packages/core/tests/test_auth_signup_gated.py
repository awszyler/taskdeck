# pyright: reportCallIssue=false
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings(*, allow_signup: bool) -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://x:y@localhost:5432/x",
        TD_RUNNER_BEARER_TOKEN="dev",
        TD_AUTH_MODE="cognito",
        TD_AUTH_ALLOW_SIGNUP=str(allow_signup).lower(),
        TD_COGNITO_USER_POOL_ID="ap-northeast-1_xxx",
        TD_COGNITO_CLIENT_ID="abc",
        TD_COGNITO_REGION="ap-northeast-1",
        TD_SESSION_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )


def _make_app(*, allow_signup: bool):
    s = _settings(allow_signup=allow_signup)
    app = create_app(s)
    app.state.settings = s
    # Stub the cognito client — signup happy path returns a fake UserSub.
    cognito = AsyncMock()
    cognito.sign_up = AsyncMock(return_value={"UserSub": "sub-fake"})
    cognito.confirm_sign_up = AsyncMock(return_value=None)
    cognito.resend_confirmation_code = AsyncMock(return_value=None)
    app.state.cognito_client = cognito
    return app, cognito


@pytest.mark.asyncio
async def test_signup_returns_403_when_disabled() -> None:
    app, cognito = _make_app(allow_signup=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/signup",
            json={"email": "a@b.c", "password": "Pa55word!"},
        )
    assert r.status_code == 403
    cognito.sign_up.assert_not_called()


@pytest.mark.asyncio
async def test_signup_returns_200_when_enabled() -> None:
    app, cognito = _make_app(allow_signup=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/signup",
            json={"email": "a@b.c", "password": "Pa55word!"},
        )
    assert r.status_code == 200
    cognito.sign_up.assert_called_once()


@pytest.mark.asyncio
async def test_signup_confirm_returns_403_when_disabled() -> None:
    app, _ = _make_app(allow_signup=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/signup/confirm",
            json={"email": "a@b.c", "code": "123456"},
        )
    assert r.status_code == 403
