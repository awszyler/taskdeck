# pyright: reportCallIssue=false
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _disabled_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://x:y@localhost:5432/x",
        TD_RUNNER_BEARER_TOKEN="dev",
        TD_AUTH_MODE="disabled",
    )


def _cognito_settings(allow_signup: bool) -> Settings:
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


def _make_app(settings: Settings):
    app = create_app(settings)
    app.state.settings = settings
    return app


@pytest.mark.asyncio
async def test_config_disabled_mode() -> None:
    app = _make_app(_disabled_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/v1/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_mode"] == "disabled"
    assert body["allow_signup"] is False
    assert body["cognito_pool_name"] is None


@pytest.mark.asyncio
async def test_config_cognito_signup_off() -> None:
    app = _make_app(_cognito_settings(allow_signup=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/v1/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_mode"] == "cognito"
    assert body["allow_signup"] is False
    # Pool name (just the SRP namespace, the part after _) is exposed.
    # Pool id and client id are NOT.
    assert body["cognito_pool_name"] == "xxx"
    assert "user_pool_id" not in r.text
    assert "client_id" not in r.text


@pytest.mark.asyncio
async def test_config_cognito_signup_on() -> None:
    app = _make_app(_cognito_settings(allow_signup=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/v1/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_mode"] == "cognito"
    assert body["allow_signup"] is True
    assert body["cognito_pool_name"] == "xxx"
